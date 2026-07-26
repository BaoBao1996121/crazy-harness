from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.control_plane.eval_campaigns import (
    EvalCampaignCreated,
    EvalCampaignIdempotencyConflict,
    EvalCampaignReport,
    EvalCampaignRequest,
)
from crazy_harness.control_plane.checkpoints import (
    CheckpointCreateRequest,
    CheckpointIdempotencyConflict,
    CheckpointRestored,
    CheckpointRestoreBlocked,
    CheckpointRestoreRequest,
    UnsafeCheckpointBoundary,
)
from crazy_harness.control_plane.model_governance import ModelBudgetConfig

from crazy_harness.control_plane.paired_evals import (
    PairedEvalCreationRejected,
    PairedEvalCreated,
    PairedEvalReport,
    PairedEvalRequest,
)
from crazy_harness.control_plane.runtime import ResidentRuntime, RunCreated, TaskRequest
from crazy_harness.control_plane.kernel import KernelDecision
from crazy_harness.core.checkpoints import CheckpointContract, CheckpointIntegrityError
from crazy_harness.control_plane.views import (
    CancelResult,
    DrainResult,
    EventPage,
    FaultResult,
    HealthView,
    RebuildResult,
    SnapshotView,
)

CONTROL_PLANE_VERSION = "0.9.0-dev"


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point: str


class PeerProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    sender: str
    receiver: str
    depth: int


class PairedEvalCreateRequest(BaseModel):
    """公开 Pair DTO；父子 Link 与释放策略只由 Harness 内部持有。"""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(
        min_length=8,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=4000)
    model_mode: Literal["scripted", "deepseek"] = "scripted"
    task_pack: Literal["repo-maintainer"] = "repo-maintainer"
    model_budget: ModelBudgetConfig = Field(default_factory=ModelBudgetConfig)

    def internal(self) -> PairedEvalRequest:
        return PairedEvalRequest.model_validate(self.model_dump(mode="python"))


class ApiErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    retryable: bool = False


class ApiErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    detail: ApiErrorDetail


def create_app(data_dir: Path, *, background: bool = True) -> FastAPI:
    runtime = ResidentRuntime(Path(data_dir))

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if background:
            runtime.start()
        yield
        runtime.stop()

    app = FastAPI(
        title="Crazy Resident A2A Control Plane",
        version=CONTROL_PLANE_VERSION,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health", response_model=HealthView)
    def health() -> HealthView:
        return HealthView.model_validate(
            {
                "status": "ok",
                "runtime": runtime.snapshot()["runtime"],
                "version": f"v{CONTROL_PLANE_VERSION}",
            }
        )

    @app.post(
        "/api/runs", status_code=status.HTTP_201_CREATED, response_model=RunCreated
    )
    def create_run(request: TaskRequest) -> RunCreated:
        try:
            return runtime.submit_task(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post(
        "/api/evals/pairs",
        status_code=status.HTTP_201_CREATED,
        response_model=PairedEvalCreated,
    )
    def create_eval_pair(request: PairedEvalCreateRequest) -> PairedEvalCreated:
        try:
            return runtime.create_paired_eval(request.internal())
        except PairedEvalCreationRejected as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/evals/pairs", response_model=list[PairedEvalReport])
    def list_eval_pairs() -> list[PairedEvalReport]:
        return runtime.eval_service.list_reports()

    @app.get("/api/evals/pairs/{eval_id}", response_model=PairedEvalReport)
    def get_eval_pair(eval_id: str) -> PairedEvalReport:
        try:
            return runtime.paired_eval(eval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="eval pair not found") from exc

    @app.post("/api/evals/pairs/{eval_id}/drain", response_model=PairedEvalReport)
    def drain_eval_pair(eval_id: str) -> PairedEvalReport:
        try:
            runtime.eval_service.contract(eval_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="eval pair not found") from exc
        runtime.run_until_idle(max_steps=300)
        return runtime.finalize_paired_eval(eval_id)

    @app.post(
        "/api/evals/campaigns",
        status_code=status.HTTP_201_CREATED,
        response_model=EvalCampaignCreated,
        responses={
            400: {"model": ApiErrorResponse, "description": "Invalid campaign"},
            409: {"model": ApiErrorResponse, "description": "Campaign conflict"},
        },
    )
    def create_eval_campaign(request: EvalCampaignRequest) -> EvalCampaignCreated:
        try:
            return runtime.create_eval_campaign(request)
        except EvalCampaignIdempotencyConflict as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": exc.code,
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "eval_campaign_creation_in_progress",
                    "message": str(exc),
                    "retryable": True,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "eval_campaign_invalid_request",
                    "message": str(exc),
                    "retryable": False,
                },
            ) from exc

    @app.get("/api/evals/campaigns", response_model=list[EvalCampaignReport])
    def list_eval_campaigns() -> list[EvalCampaignReport]:
        return runtime.campaign_service.list_reports()

    @app.get(
        "/api/evals/campaigns/{campaign_id}",
        response_model=EvalCampaignReport,
        responses={
            404: {"model": ApiErrorResponse, "description": "Campaign not found"},
        },
    )
    def get_eval_campaign(campaign_id: str) -> EvalCampaignReport:
        try:
            return runtime.eval_campaign(campaign_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "eval_campaign_not_found",
                    "message": "campaign not found",
                    "retryable": False,
                },
            ) from exc

    @app.post(
        "/api/evals/campaigns/{campaign_id}/drain",
        response_model=EvalCampaignReport,
        responses={
            404: {"model": ApiErrorResponse, "description": "Campaign not found"},
        },
    )
    def drain_eval_campaign(campaign_id: str) -> EvalCampaignReport:
        try:
            runtime.campaign_service.contract(campaign_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "eval_campaign_not_found",
                    "message": "campaign not found",
                    "retryable": False,
                },
            ) from exc
        runtime.run_eval_campaign_until_idle(campaign_id, max_steps=1500)
        return runtime.finalize_eval_campaign(campaign_id)

    @app.post(
        "/api/evals/campaigns/{campaign_id}/cancel",
        response_model=EvalCampaignReport,
        responses={
            404: {"model": ApiErrorResponse, "description": "Campaign not found"},
            409: {"model": ApiErrorResponse, "description": "Cancellation conflict"},
        },
    )
    def cancel_eval_campaign(campaign_id: str) -> EvalCampaignReport:
        try:
            return runtime.cancel_eval_campaign(campaign_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "eval_campaign_not_found",
                    "message": "campaign not found",
                    "retryable": False,
                },
            ) from exc
        except TimeoutError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "eval_campaign_cancellation_in_progress",
                    "message": str(exc),
                    "retryable": True,
                },
            ) from exc

    @app.post("/api/runs/{run_id}/drain", response_model=DrainResult)
    def drain_run(run_id: str) -> DrainResult:
        if runtime.store.projection("run", run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return DrainResult(run_id=run_id, steps=runtime.run_until_idle(max_steps=150))

    @app.post("/api/runs/{run_id}/cancel", response_model=CancelResult)
    def cancel_run(run_id: str) -> CancelResult:
        try:
            return CancelResult.model_validate(runtime.cancel_run(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post(
        "/api/runs/{run_id}/checkpoints",
        status_code=status.HTTP_201_CREATED,
        response_model=CheckpointContract,
    )
    def create_checkpoint(
        run_id: str,
        request: CheckpointCreateRequest,
    ) -> CheckpointContract:
        try:
            return runtime.create_checkpoint(run_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except (UnsafeCheckpointBoundary, CheckpointIdempotencyConflict) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        "/api/runs/{run_id}/checkpoints",
        response_model=list[CheckpointContract],
    )
    def list_checkpoints(run_id: str) -> list[CheckpointContract]:
        if runtime.store.projection("run", run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        return runtime.checkpoints.list_for_run(run_id)

    @app.get(
        "/api/checkpoints/{checkpoint_id}",
        response_model=CheckpointContract,
    )
    def get_checkpoint(checkpoint_id: str) -> CheckpointContract:
        try:
            return runtime.checkpoints.contract(checkpoint_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="checkpoint not found") from exc

    @app.post(
        "/api/checkpoints/{checkpoint_id}/restore",
        response_model=CheckpointRestored,
    )
    def restore_checkpoint(
        checkpoint_id: str,
        request: CheckpointRestoreRequest,
    ) -> CheckpointRestored:
        try:
            return runtime.restore_checkpoint(checkpoint_id, request)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="checkpoint not found") from exc
        except (CheckpointRestoreBlocked, CheckpointIntegrityError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/snapshot", response_model=SnapshotView)
    def snapshot(run_id: str | None = None) -> SnapshotView:
        result = runtime.snapshot(run_id)
        if run_id is not None and result["run"] is None:
            raise HTTPException(status_code=404, detail="run not found")
        return SnapshotView.model_validate(result)

    @app.get("/api/events", response_model=EventPage)
    def events(
        after: int = Query(default=0, ge=0),
        run_id: str | None = None,
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> EventPage:
        records = runtime.store.read_records(after=after, run_id=run_id, limit=limit)
        next_cursor = records[-1].cursor if records else after
        return EventPage.model_validate(
            {
                "items": [record.model_dump(mode="json") for record in records],
                "next_cursor": next_cursor,
            }
        )

    @app.get("/api/events/stream")
    def stream_events(
        after: int = Query(default=0, ge=0),
        run_id: str | None = None,
        once: bool = False,
    ) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            cursor = after
            while True:
                records = runtime.store.read_records(
                    after=cursor, run_id=run_id, limit=500
                )
                for record in records:
                    cursor = record.cursor
                    data = {
                        "cursor": record.cursor,
                        "type": record.event.type,
                        "event": record.event.model_dump(mode="json"),
                    }
                    encoded = json.dumps(
                        data, ensure_ascii=False, separators=(",", ":")
                    )
                    yield f"id: {record.cursor}\nevent: runtime\ndata: {encoded}\n\n"
                if once:
                    break
                if not records:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/chaos/faults", response_model=FaultResult)
    def arm_fault(request: FaultRequest) -> FaultResult:
        try:
            runtime.arm_fault(request.point)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return FaultResult(armed=request.point, mode="one_shot")

    @app.post("/api/chaos/peer-probe", response_model=KernelDecision)
    def peer_probe(request: PeerProbeRequest) -> KernelDecision:
        try:
            return runtime.submit_peer_probe(
                request.run_id,
                sender=request.sender,
                receiver=request.receiver,
                depth=request.depth,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/projections/rebuild", response_model=RebuildResult)
    def rebuild_projections() -> RebuildResult:
        runtime.store.rebuild_projections()
        return RebuildResult(status="rebuilt")

    frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount(
            "/", StaticFiles(directory=frontend_dist, html=True), name="control-room"
        )
    return app
