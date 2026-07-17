from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from crazy_harness.core.agents import AgentLoop, AssignmentContract
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event
from crazy_harness.core.models import ModelProvider
from crazy_harness.core.skills import SkillCatalog


class PreparedTaskWorkspace(Protocol):
    workspace: Path


class TaskPack(Protocol):
    task_pack_id: str
    agent_id: str

    def prepare(self, run_id: str) -> PreparedTaskWorkspace: ...

    def assignment_contract(self) -> AssignmentContract: ...

    def scripted_responses(self) -> list[str]: ...

    def build_loop(
        self,
        *,
        run_id: str,
        task_id: str,
        brief: str,
        model_mode: str,
        model: ModelProvider,
        event_log,
        artifact_store: ArtifactStore,
        ledger_path: Path,
        assignment_contract: AssignmentContract | None = None,
        fault_injector: Callable[[str], None] | None = None,
    ) -> AgentLoop: ...


def record_skill_catalog(
    event_log,
    *,
    run_id: str,
    task_id: str,
    agent_id: str,
    source: str,
    skills: SkillCatalog,
) -> None:
    """Persist a body-free Skill catalog once per stable manifest."""

    manifest = skills.audit_manifest()
    existing = [
        event
        for event in event_log.read_all(task_id=task_id)
        if event.type == "skill.catalog.compiled"
    ]
    if (
        existing
        and existing[-1].payload.get("manifest_hash") == manifest["manifest_hash"]
    ):
        return
    parent = event_log.last(task_id=task_id)
    event_log.append(
        Event(
            run_id=run_id,
            task_id=task_id,
            type="skill.catalog.compiled",
            source=source,
            payload={
                "agent_id": agent_id,
                "disclosure": "metadata_then_explicit_activation",
                **manifest,
            },
            causation_id=parent.id if parent is not None else None,
        )
    )
