from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from crazy_harness.control_plane.engineering_loops import EngineeringLoopReport
from crazy_harness.control_plane.runtime import ResidentRuntime


DSH_PROTOCOL_VERSION = "crazy-dsh-v1"


class DshIntegrationCapabilities(BaseModel):
    """Version handshake for out-of-tree DeepSeek Harness plugins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["crazy-dsh-v1"] = DSH_PROTOCOL_VERSION
    control_plane_version: str
    transport: Literal["http-json"] = "http-json"
    capabilities: tuple[str, ...] = (
        "engineering_loop.read",
    )


class DshEngineeringLoopView(BaseModel):
    """Stable DSH projection that does not expose internal loop authority fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    loop_id: str
    title: str
    objective: str
    status: Literal[
        "running",
        "pausing",
        "paused",
        "resuming",
        "awaiting_approval",
        "completed",
        "blocked",
        "cancelled",
    ]
    active_score: str | None = None
    iteration_count: int
    child_run_ids: tuple[str, ...] = ()
    terminal_reason: str | None = None

    @classmethod
    def from_report(cls, report: EngineeringLoopReport) -> DshEngineeringLoopView:
        return cls(
            loop_id=report.loop_id,
            title=report.contract.title,
            objective=report.contract.objective,
            status=report.status,
            active_score=(str(report.active_score) if report.active_score is not None else None),
            iteration_count=len(report.iterations),
            child_run_ids=tuple(
                iteration.identity.child_run_id for iteration in report.iterations
            ),
            terminal_reason=report.terminal_reason,
        )


def create_dsh_integration_router(
    runtime: ResidentRuntime,
    *,
    control_plane_version: str,
) -> APIRouter:
    """Expose versioned, side-effect-free projections to DSH plugins."""

    router = APIRouter(prefix="/api/integrations/dsh/v1", tags=["dsh-integration"])

    @router.get("/capabilities", response_model=DshIntegrationCapabilities)
    def capabilities() -> DshIntegrationCapabilities:
        return DshIntegrationCapabilities(
            control_plane_version=control_plane_version,
        )

    @router.get(
        "/engineering-loops/{loop_id}",
        response_model=DshEngineeringLoopView,
    )
    def get_engineering_loop(loop_id: str) -> DshEngineeringLoopView:
        try:
            return DshEngineeringLoopView.from_report(runtime.engineering_loop(loop_id))
        except KeyError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "code": "engineering_loop_not_found",
                    "message": "engineering loop not found",
                    "retryable": False,
                },
            ) from exc

    return router
