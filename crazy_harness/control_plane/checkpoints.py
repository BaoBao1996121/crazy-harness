from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.control_plane.store import EventRecord, SQLiteEventStore
from crazy_harness.core.artifacts import ArtifactRef
from crazy_harness.core.checkpoints import (
    CheckpointContract,
    CheckpointEffect,
    CheckpointEffectBoundary,
    CheckpointIntegrityError,
    CheckpointSourceBoundary,
    CheckpointStateRefs,
    VerifiedArtifactRef,
    WorkspaceSnapshotStore,
)
from crazy_harness.core.events import Event


class CheckpointCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)
    label: str = Field(default="", max_length=200)


class CheckpointRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=128)


class CheckpointRestored(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str
    source_run_id: str
    run_id: str
    task_id: str
    status: Literal["queued"] = "queued"


class CheckpointServiceError(RuntimeError):
    pass


class CheckpointIdempotencyConflict(CheckpointServiceError):
    pass


class UnsafeCheckpointBoundary(CheckpointServiceError):
    pass


class CheckpointRestoreBlocked(CheckpointServiceError):
    pass


class CheckpointService:
    """Durable Checkpoint contracts backed by EventStore and immutable workspace objects."""

    _PLAN_EVENTS = {
        "plan.created",
        "plan.revised",
        "step.started",
        "step.completed",
        "step.cancelled",
        "step.superseded",
    }
    _LOCAL_EFFECTS = {"none", "browser_read", "workspace_write", "local_process"}

    def __init__(
        self,
        store: SQLiteEventStore,
        snapshots: WorkspaceSnapshotStore,
        *,
        artifact_root: Path,
    ) -> None:
        self.store = store
        self.snapshots = snapshots
        self.artifact_root = artifact_root.resolve()

    def fork_blocker(self, run_id: str) -> str | None:
        """Return why the current durable boundary cannot be safely restored."""

        records = self.store.read_records(run_id=run_id)
        try:
            self._assert_safe(records)
        except UnsafeCheckpointBoundary as exc:
            return str(exc)
        effects = self._effect_boundary([record.event for record in records])
        if effects.restore_blockers:
            return "checkpoint restore is blocked: " + ", ".join(
                effects.restore_blockers
            )
        return None

    def create(
        self,
        run_id: str,
        request: CheckpointCreateRequest,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> CheckpointContract:
        checkpoint_id = self.checkpoint_id(run_id, request.request_id)
        fingerprint = self._request_fingerprint(run_id, request)
        existing = self._requested_event(checkpoint_id)
        if existing is not None:
            contract = CheckpointContract.model_validate(existing.payload["contract"])
            if contract.request_fingerprint != fingerprint:
                raise CheckpointIdempotencyConflict(
                    f"checkpoint request_id belongs to another payload: {request.request_id}"
                )
            self._commit(contract)
            return contract

        records = self.store.read_records(run_id=run_id)
        self._assert_safe(records)
        run_created = next(
            (record.event for record in records if record.event.type == "run.created"),
            None,
        )
        if run_created is None:
            raise KeyError(f"run has no run.created event: {run_id}")
        workspace = Path(str(run_created.payload.get("workspace_path", "")))
        snapshot = self.snapshots.create(workspace)
        contract = self._build_contract(
            checkpoint_id=checkpoint_id,
            request=request,
            fingerprint=fingerprint,
            records=records,
            snapshot=snapshot,
            run_created=run_created,
        )
        requested = self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"crazy:{checkpoint_id}:requested")),
                run_id=contract.source.run_id,
                task_id=contract.source.task_id,
                type="checkpoint.requested",
                source="checkpoint.service",
                payload={"contract": contract.model_dump(mode="json")},
                causation_id=contract.source.event_id,
            )
        )
        if fault_injector is not None:
            fault_injector("after_checkpoint_requested")
        self._commit(contract, causation_id=requested.id)
        return contract

    def contract(self, checkpoint_id: str) -> CheckpointContract:
        requested = self._requested_event(checkpoint_id)
        if requested is None:
            raise KeyError(f"unknown checkpoint: {checkpoint_id}")
        contract = CheckpointContract.model_validate(requested.payload["contract"])
        committed = self._committed_event(checkpoint_id)
        if committed is None:
            raise KeyError(f"checkpoint is not committed: {checkpoint_id}")
        if committed.payload.get("contract_sha256") != self._contract_digest(contract):
            raise CheckpointIntegrityError("checkpoint contract hash mismatch")
        return contract

    def list_for_run(self, run_id: str) -> list[CheckpointContract]:
        contracts: list[CheckpointContract] = []
        for event in self.store.read_all(run_id=run_id):
            if event.type != "checkpoint.requested":
                continue
            contract = CheckpointContract.model_validate(event.payload["contract"])
            if self._committed_event(contract.checkpoint_id) is not None:
                contracts.append(self.contract(contract.checkpoint_id))
        return contracts

    def validate_restore(self, checkpoint_id: str) -> CheckpointContract:
        contract = self.contract(checkpoint_id)
        records = self.store.read_records(run_id=contract.source.run_id)
        prefix = records[: contract.source.event_count]
        if (
            len(prefix) != contract.source.event_count
            or prefix[-1].cursor != contract.source.event_cursor
            or prefix[-1].event.id != contract.source.event_id
            or self._prefix_digest(prefix) != contract.source.event_prefix_sha256
        ):
            raise CheckpointIntegrityError("checkpoint event prefix mismatch")
        self.snapshots.verify(contract.workspace)
        for artifact in contract.state_refs.artifacts:
            self._verify_artifact(artifact)
        if contract.effects.restore_blockers:
            blockers = ", ".join(contract.effects.restore_blockers)
            raise CheckpointRestoreBlocked(f"checkpoint restore is blocked: {blockers}")
        return contract

    def _build_contract(
        self,
        *,
        checkpoint_id: str,
        request: CheckpointCreateRequest,
        fingerprint: str,
        records: list[EventRecord],
        snapshot,
        run_created: Event,
    ) -> CheckpointContract:
        events = [record.event for record in records]
        assignment = self._latest(events, {"assignment.created"})
        plan = self._latest(events, self._PLAN_EVENTS)
        context = self._latest(events, {"context.manifest.compiled"})
        last = records[-1]
        turn_event = next(
            (event for event in reversed(events) if event.payload.get("turn_id")),
            None,
        )
        phase_event = next(
            (
                event
                for event in reversed(events)
                if isinstance(event.payload.get("phase"), str)
                and event.payload.get("phase")
            ),
            None,
        )
        return CheckpointContract(
            checkpoint_id=checkpoint_id,
            request_id=request.request_id,
            request_fingerprint=fingerprint,
            label=request.label,
            task_pack=str(run_created.payload.get("task_pack", "unknown")),
            baseline_identity=self._baseline_identity(run_created),
            source=CheckpointSourceBoundary(
                run_id=last.event.run_id,
                task_id=last.event.task_id,
                event_id=last.event.id,
                event_cursor=last.cursor,
                event_count=len(records),
                event_prefix_sha256=self._prefix_digest(records),
                turn_id=(str(turn_event.payload["turn_id"]) if turn_event is not None else None),
                phase=(
                    str(phase_event.payload["phase"])
                    if phase_event is not None
                    else last.event.type
                ),
            ),
            workspace=snapshot,
            state_refs=CheckpointStateRefs(
                run_created_event_id=run_created.id,
                assignment_event_id=assignment.id if assignment is not None else None,
                local_plan_event_id=plan.id if plan is not None else None,
                context_manifest_event_id=context.id if context is not None else None,
                artifacts=self._artifact_refs(events),
            ),
            effects=self._effect_boundary(events),
        )

    def _commit(
        self,
        contract: CheckpointContract,
        *,
        causation_id: str | None = None,
    ) -> Event:
        self.snapshots.verify(contract.workspace)
        for artifact in contract.state_refs.artifacts:
            self._verify_artifact(artifact)
        requested = self._requested_event(contract.checkpoint_id)
        return self.store.append(
            Event(
                id=str(uuid5(NAMESPACE_URL, f"crazy:{contract.checkpoint_id}:committed")),
                run_id=contract.source.run_id,
                task_id=contract.source.task_id,
                type="checkpoint.committed",
                source="checkpoint.service",
                payload={
                    "checkpoint_id": contract.checkpoint_id,
                    "contract_sha256": self._contract_digest(contract),
                },
                causation_id=causation_id or (requested.id if requested is not None else None),
            )
        )

    def _assert_safe(self, records: list[EventRecord]) -> None:
        if not records:
            raise UnsafeCheckpointBoundary("run has no durable events")
        events = [record.event for record in records]
        started = {
            str(event.payload.get("operation_id"))
            for event in events
            if event.type == "operation.started"
        }
        terminal = {
            str(event.payload.get("operation_id"))
            for event in events
            if event.type in {"operation.completed", "operation.failed", "operation.unknown"}
        }
        unresolved = sorted(started - terminal)
        if unresolved:
            raise UnsafeCheckpointBoundary(
                f"unresolved operation prevents checkpoint: {', '.join(unresolved)}"
            )
        requested_tools = {
            str(event.payload.get("operation_id"))
            for event in events
            if event.type == "tool.requested"
        }
        terminal_tools = {
            str(event.payload.get("operation_id"))
            for event in events
            if event.type in {"tool.completed", "tool.failed"}
        }
        unresolved_tools = sorted(requested_tools - terminal_tools)
        if unresolved_tools:
            raise UnsafeCheckpointBoundary(
                "unresolved tool request prevents checkpoint: "
                + ", ".join(unresolved_tools)
            )
        requested_turns = {
            str(event.payload.get("turn_id"))
            for event in events
            if event.type == "model.requested"
        }
        completed_turns = {
            str(event.payload.get("turn_id"))
            for event in events
            if event.type == "model.completed"
        }
        if requested_turns - completed_turns:
            raise UnsafeCheckpointBoundary("unresolved model call prevents checkpoint")

    def _effect_boundary(self, events: list[Event]) -> CheckpointEffectBoundary:
        terminals = {
            str(event.payload.get("operation_id")): event
            for event in events
            if event.type in {"operation.completed", "operation.failed", "operation.unknown"}
        }
        effects: list[CheckpointEffect] = []
        blockers: list[str] = []
        for event in events:
            if event.type != "operation.started":
                continue
            operation_id = str(event.payload.get("operation_id"))
            terminal = terminals[operation_id]
            state = terminal.type.removeprefix("operation.")
            level = str(event.payload.get("side_effect_level", "unknown"))
            if state == "failed":
                disposition = "no_effect"
            elif state == "unknown":
                disposition = "unknown"
            elif level in {"none", "browser_read"}:
                disposition = "read_only"
            elif level in {"workspace_write", "local_process"}:
                disposition = "workspace_restored"
            elif level == "irreversible":
                disposition = "irreversible"
            elif level in {"idempotent_external", "compensatable"}:
                disposition = "requires_reconciliation"
            else:
                disposition = "unknown"
            effect = CheckpointEffect(
                operation_id=operation_id,
                tool_name=str(event.payload.get("tool_name", "unknown")),
                terminal_state=state,
                side_effect_level=level,
                disposition=disposition,
                idempotency_key=(
                    str(event.payload["idempotency_key"])
                    if event.payload.get("idempotency_key")
                    else None
                ),
            )
            effects.append(effect)
            if disposition in {"unknown", "irreversible", "requires_reconciliation"}:
                blockers.append(f"operation:{operation_id}:{disposition}")
        return CheckpointEffectBoundary(
            effects=tuple(effects),
            restore_blockers=tuple(blockers),
        )

    def _artifact_refs(self, events: list[Event]) -> tuple[VerifiedArtifactRef, ...]:
        artifacts: dict[str, VerifiedArtifactRef] = {}
        for event in events:
            for raw in self._find_artifact_refs(event.payload):
                ref = ArtifactRef.model_validate(raw)
                path = self._managed_artifact_path(ref.uri)
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise CheckpointIntegrityError(f"checkpoint artifact is unreadable: {ref.uri}") from exc
                artifact_id = path.relative_to(self.artifact_root).as_posix()
                artifacts.setdefault(
                    artifact_id,
                    VerifiedArtifactRef(
                        artifact_id=artifact_id,
                        kind=ref.kind,
                        summary=ref.summary,
                        source_event_id=event.id,
                        size=len(content),
                        sha256=sha256(content).hexdigest(),
                    ),
                )
        return tuple(artifacts.values())

    @classmethod
    def _find_artifact_refs(cls, value: Any) -> Iterable[dict[str, Any]]:
        if isinstance(value, dict):
            candidate = value.get("artifact_ref")
            if isinstance(candidate, dict) and {"uri", "kind"} <= set(candidate):
                yield candidate
            for nested in value.values():
                yield from cls._find_artifact_refs(nested)
        elif isinstance(value, list):
            for nested in value:
                yield from cls._find_artifact_refs(nested)

    def _verify_artifact(self, artifact: VerifiedArtifactRef) -> None:
        path = self._managed_artifact_path(self.artifact_root / artifact.artifact_id)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise CheckpointIntegrityError(
                f"checkpoint artifact is unreadable: {artifact.artifact_id}"
            ) from exc
        if len(content) != artifact.size or sha256(content).hexdigest() != artifact.sha256:
            raise CheckpointIntegrityError(
                f"checkpoint artifact hash mismatch: {artifact.artifact_id}"
            )

    def _managed_artifact_path(self, value: str | Path) -> Path:
        try:
            path = Path(value).resolve(strict=True)
            path.relative_to(self.artifact_root)
        except (OSError, ValueError) as exc:
            raise CheckpointIntegrityError(
                "checkpoint artifact is outside managed root"
            ) from exc
        return path

    @staticmethod
    def _baseline_identity(run_created: Event) -> str:
        fixture_hash = run_created.payload.get("fixture_hash")
        if isinstance(fixture_hash, str) and len(fixture_hash) == 64:
            return f"fixture_sha256:{fixture_hash}"
        task_pack = str(run_created.payload.get("task_pack", "unknown"))
        return f"task_pack:{task_pack}:baseline_unspecified"

    def _requested_event(self, checkpoint_id: str) -> Event | None:
        matches = self.store.find(
            lambda event: event.type == "checkpoint.requested"
            and event.payload.get("contract", {}).get("checkpoint_id") == checkpoint_id
        )
        return matches[-1] if matches else None

    def _committed_event(self, checkpoint_id: str) -> Event | None:
        matches = self.store.find(
            lambda event: event.type == "checkpoint.committed"
            and event.payload.get("checkpoint_id") == checkpoint_id
        )
        return matches[-1] if matches else None

    @staticmethod
    def _latest(events: list[Event], event_types: set[str]) -> Event | None:
        return next((event for event in reversed(events) if event.type in event_types), None)

    @staticmethod
    def _prefix_digest(records: list[EventRecord]) -> str:
        payload = [
            {"cursor": record.cursor, "event": record.event.model_dump(mode="json")}
            for record in records
        ]
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _request_fingerprint(run_id: str, request: CheckpointCreateRequest) -> str:
        payload = {"run_id": run_id, **request.model_dump(mode="json")}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _contract_digest(contract: CheckpointContract) -> str:
        encoded = json.dumps(
            contract.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def checkpoint_id(run_id: str, request_id: str) -> str:
        return f"checkpoint_{uuid5(NAMESPACE_URL, f'crazy:checkpoint:{run_id}:{request_id}').hex}"
