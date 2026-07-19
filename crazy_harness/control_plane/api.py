from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from crazy_harness.control_plane.paired_evals import (
    PairedEvalCreationRejected,
    PairedEvalCreated,
    PairedEvalReport,
    PairedEvalRequest,
)
from crazy_harness.control_plane.runtime import ResidentRuntime, RunCreated, TaskRequest
from crazy_harness.control_plane.kernel import KernelDecision
from crazy_harness.control_plane.views import (
    CancelResult,
    DrainResult,
    EventPage,
    FaultResult,
    HealthView,
    RebuildResult,
    SnapshotView,
)

CONTROL_PLANE_VERSION = "0.8.0-dev"


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point: str


class PeerProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    sender: str
    receiver: str
    depth: int


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
        return HealthView.model_validate({
            "status": "ok",
            "runtime": runtime.snapshot()["runtime"],
            "version": f"v{CONTROL_PLANE_VERSION}",
        })

    @app.post("/api/runs", status_code=status.HTTP_201_CREATED, response_model=RunCreated)
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
    def create_eval_pair(request: PairedEvalRequest) -> PairedEvalCreated:
        try:
            return runtime.create_paired_eval(request)
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
        return EventPage.model_validate({
            "items": [record.model_dump(mode="json") for record in records],
            "next_cursor": next_cursor,
        })

    @app.get("/api/events/stream")
    def stream_events(
        after: int = Query(default=0, ge=0),
        run_id: str | None = None,
        once: bool = False,
    ) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            cursor = after
            while True:
                records = runtime.store.read_records(after=cursor, run_id=run_id, limit=500)
                for record in records:
                    cursor = record.cursor
                    data = {
                        "cursor": record.cursor,
                        "type": record.event.type,
                        "event": record.event.model_dump(mode="json"),
                    }
                    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
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
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="control-room")
    return app
