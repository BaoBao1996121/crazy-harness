from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.events import Event


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventRecord(BaseModel):
    """An Event plus its stable, monotonically increasing SSE cursor."""

    model_config = ConfigDict(frozen=True)

    cursor: int = Field(ge=1)
    event: Event


class CommandPreconditionFailed(RuntimeError):
    """A dynamic command invariant changed before its atomic commit."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SQLiteEventStore:
    """SQLite event log, command ledger, and rebuildable read projections."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._condition = threading.Condition()
        self._transaction_state = threading.local()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        active = getattr(self._transaction_state, "connection", None)
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=NORMAL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            # WAL + NORMAL 覆盖进程崩溃恢复；极端主机掉电零丢失不属于本地教学版承诺。
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run_seq ON events(run_id, seq);
                CREATE INDEX IF NOT EXISTS events_task_seq ON events(task_id, seq);
                CREATE INDEX IF NOT EXISTS events_type_seq ON events(event_type, seq);
                CREATE TABLE IF NOT EXISTS commands (
                    idempotency_key TEXT PRIMARY KEY,
                    candidate_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    decision_json TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projections (
                    kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_seq INTEGER NOT NULL,
                    PRIMARY KEY(kind, entity_id)
                );
                """
            )

    def append(self, event: Event) -> Event:
        """Append once by Event.id and update projections in the same transaction."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            persisted, inserted = self._append_in_transaction(connection, event)
            connection.commit()
        if inserted:
            with self._condition:
                self._condition.notify_all()
        return persisted

    def _append_in_transaction(
        self,
        connection: sqlite3.Connection,
        event: Event,
    ) -> tuple[Event, bool]:
        serialized = event.model_dump_json()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO events(event_id, run_id, task_id, event_type, event_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event.id, event.run_id, event.task_id, event.type, serialized),
        )
        if cursor.rowcount:
            self._project_event(connection, event, int(cursor.lastrowid))
            return event, True
        row = connection.execute(
            "SELECT event_json FROM events WHERE event_id = ?", (event.id,)
        ).fetchone()
        existing = Event.model_validate_json(row["event_json"]) if row else None
        if existing is None or existing.model_dump(
            exclude={"created_at"}
        ) != event.model_dump(exclude={"created_at"}):
            raise ValueError(f"event id already belongs to another event: {event.id}")
        return existing, False

    def read_records(
        self,
        *,
        after: int = 0,
        run_id: str | None = None,
        task_id: str | None = None,
        limit: int | None = None,
    ) -> list[EventRecord]:
        clauses = ["seq > ?"]
        values: list[Any] = [after]
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        if task_id is not None:
            clauses.append("task_id = ?")
            values.append(task_id)
        sql = f"SELECT seq, event_json FROM events WHERE {' AND '.join(clauses)} ORDER BY seq"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [
            EventRecord(cursor=int(row["seq"]), event=Event.model_validate_json(row["event_json"]))
            for row in rows
        ]

    def read_all(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Event]:
        return [record.event for record in self.read_records(task_id=task_id, run_id=run_id)]

    def last(
        self,
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> Event | None:
        clauses: list[str] = []
        values: list[Any] = []
        if task_id is not None:
            clauses.append("task_id = ?")
            values.append(task_id)
        if run_id is not None:
            clauses.append("run_id = ?")
            values.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT event_json FROM events {where} ORDER BY seq DESC LIMIT 1", values
            ).fetchone()
        return Event.model_validate_json(row["event_json"]) if row else None

    def find(
        self,
        predicate: Callable[[Event], bool],
        *,
        task_id: str | None = None,
        run_id: str | None = None,
    ) -> list[Event]:
        return [event for event in self.read_all(task_id=task_id, run_id=run_id) if predicate(event)]

    def get_event(self, event_id: str) -> Event:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT event_json FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown event: {event_id}")
        return Event.model_validate_json(row["event_json"])

    def wait_for_records(self, *, after: int, timeout: float = 1.0) -> list[EventRecord]:
        records = self.read_records(after=after)
        if records:
            return records
        with self._condition:
            self._condition.wait(timeout)
        return self.read_records(after=after)

    def begin_command(self, *, idempotency_key: str, candidate_id: str, candidate_json: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO commands(
                    idempotency_key, candidate_id, state, candidate_json, updated_at
                ) VALUES (?, ?, 'processing', ?, ?)
                """,
                (idempotency_key, candidate_id, candidate_json, _utc_now()),
            )
            return bool(cursor.rowcount)

    def command_record(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM commands WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return dict(row) if row else None

    def commit_command(
        self,
        idempotency_key: str,
        *,
        state: str,
        decision_json: str,
        events: list[Event],
        after_event: Callable[[Event], None] | None = None,
        precondition: Callable[[], str | None] | None = None,
    ) -> list[Event]:
        """Atomically append formal facts, update projections, and finalize a command."""

        persisted: list[Event] = []
        inserted_any = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # BEGIN IMMEDIATE serializes competing writers. Re-reading dynamic
            # authority here closes the validation/commit TOCTOU window.
            if precondition is not None:
                previous = getattr(self._transaction_state, "connection", None)
                self._transaction_state.connection = connection
                try:
                    rejection = precondition()
                finally:
                    self._transaction_state.connection = previous
                if rejection is not None:
                    raise CommandPreconditionFailed(rejection)
            for event in events:
                stored, inserted = self._append_in_transaction(connection, event)
                persisted.append(stored)
                inserted_any = inserted_any or inserted
                if after_event is not None:
                    after_event(stored)
            cursor = connection.execute(
                """
                UPDATE commands SET state = ?, decision_json = ?, updated_at = ?
                WHERE idempotency_key = ? AND state = 'processing'
                """,
                (state, decision_json, _utc_now(), idempotency_key),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT state, decision_json FROM commands WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if (
                    row is None
                    or row["state"] != state
                    or row["decision_json"] != decision_json
                ):
                    raise RuntimeError(
                        f"command could not be finalized: {idempotency_key}"
                    )
            connection.commit()
        if inserted_any:
            with self._condition:
                self._condition.notify_all()
        return persisted

    def finish_command(self, idempotency_key: str, *, state: str, decision_json: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE commands SET state = ?, decision_json = ?, updated_at = ?
                WHERE idempotency_key = ? AND state = 'processing'
                """,
                (state, decision_json, _utc_now(), idempotency_key),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT state, decision_json FROM commands WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row is None or row["decision_json"] != decision_json:
                    raise RuntimeError(f"command could not be finalized: {idempotency_key}")

    def snapshot(self, *, run_id: str | None = None) -> dict[str, list[dict[str, Any]]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT kind, state_json FROM projections ORDER BY kind, entity_id"
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {
            "runs": [],
            "agents": [],
            "assignments": [],
            "leases": [],
            "contexts": [],
            "capability_manifests": [],
            "memories": [],
            "evolutions": [],
            "dream_jobs": [],
        }
        plural = {
            "run": "runs",
            "agent": "agents",
            "assignment": "assignments",
            "lease": "leases",
            "context": "contexts",
            "capability_manifest": "capability_manifests",
            "memory": "memories",
            "evolution": "evolutions",
            "dream_job": "dream_jobs",
        }
        for row in rows:
            state = json.loads(row["state_json"])
            if run_id is not None and row["kind"] != "agent" and state.get("run_id") != run_id:
                continue
            key = plural.get(row["kind"])
            if key is not None:
                grouped[key].append(state)
        return grouped

    def projection(self, kind: str, entity_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._load_projection(connection, kind, entity_id)

    def clear_projections(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM projections")

    def rebuild_projections(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM projections")
            rows = connection.execute("SELECT seq, event_json FROM events ORDER BY seq").fetchall()
            for row in rows:
                self._project_event(
                    connection,
                    Event.model_validate_json(row["event_json"]),
                    int(row["seq"]),
                )
            connection.commit()

    def _project_event(self, connection: sqlite3.Connection, event: Event, seq: int) -> None:
        run = self._load_projection(connection, "run", event.run_id)
        if event.type == "run.created":
            run = {
                "run_id": event.run_id,
                "task_id": event.task_id,
                "title": event.payload.get("title", "Untitled run"),
                "brief": event.payload.get("brief", ""),
                "status": "running",
                "phase": "intake",
                "model_mode": event.payload.get("model_mode", "scripted"),
                "behavior_version": event.payload.get("behavior_version", "v0.1.0"),
                "started_at": event.created_at.isoformat(),
                "event_count": 0,
            }
        if run is not None:
            run["event_count"] = int(run.get("event_count", 0)) + 1
            run["last_event_type"] = event.type
            run["last_cursor"] = seq
            run["updated_at"] = event.created_at.isoformat()
            if event.type == "run.phase.changed":
                run["phase"] = event.payload.get("phase", run.get("phase"))
            elif event.type == "run.succeeded":
                run.update(status="succeeded", phase="complete", completed_at=event.created_at.isoformat())
            elif event.type == "run.failed":
                run.update(status="failed", phase="failed", completed_at=event.created_at.isoformat())
            elif event.type == "run.paused":
                run["status"] = "paused"
            elif event.type == "completion.gate.passed":
                run["completion_gate"] = "passed"
            elif event.type == "completion.gate.failed":
                run["completion_gate"] = "failed"
            self._save_projection(connection, "run", event.run_id, run, seq)

        if event.type == "agent.registered":
            agent_id = str(event.payload["agent_id"])
            self._save_projection(
                connection,
                "agent",
                agent_id,
                {
                    "agent_id": agent_id,
                    "role": event.payload.get("role", agent_id.title()),
                    "capabilities": event.payload.get("capabilities", []),
                    "status": "idle",
                    "max_concurrency": int(event.payload.get("max_concurrency", 1)),
                    "mailbox_pending": 0,
                    "updated_at": event.created_at.isoformat(),
                },
                seq,
            )
        elif event.type.startswith("runtime.agent."):
            self._project_agent_runtime(connection, event, seq)
        elif event.type in {"mailbox.delivery.sent", "mailbox.delivery.acked"}:
            self._project_mailbox(connection, event, seq)

        if event.type == "assignment.created":
            assignment_id = str(event.payload["assignment_id"])
            state = dict(event.payload)
            state.update(
                run_id=event.run_id,
                task_id=event.task_id,
                status="queued",
                updated_at=event.created_at.isoformat(),
            )
            self._save_projection(connection, "assignment", assignment_id, state, seq)
        else:
            status_by_type = {
                "assignment.running": "running",
                "assignment.waiting": "waiting",
                "assignment.reviewing": "reviewing",
                "assignment.submitted": "submitted",
                "assignment.succeeded": "succeeded",
                "assignment.completed": "completed",
                "assignment.failed": "failed",
                "assignment.expired": "expired",
            }
            assignment_id = event.payload.get("assignment_id")
            status = status_by_type.get(event.type)
            if assignment_id and status is not None:
                state = self._load_projection(connection, "assignment", str(assignment_id))
                if state is not None:
                    state["status"] = status
                    state["updated_at"] = event.created_at.isoformat()
                    self._save_projection(connection, "assignment", str(assignment_id), state, seq)

        if event.type.startswith("assignment.lease."):
            self._project_lease(connection, event, seq)

        if event.type in {"context.compiled", "context.manifest.compiled"}:
            agent_id = str(event.payload["agent_id"])
            state = dict(event.payload)
            state.update(run_id=event.run_id, task_id=event.task_id, updated_at=event.created_at.isoformat())
            self._save_projection(connection, "context", f"{event.run_id}:{agent_id}", state, seq)

        if event.type == "capability.manifest.compiled":
            agent_id = str(event.payload["agent_id"])
            state = dict(event.payload)
            state.update(run_id=event.run_id, task_id=event.task_id, updated_at=event.created_at.isoformat())
            self._save_projection(
                connection,
                "capability_manifest",
                f"{event.run_id}:{agent_id}",
                state,
                seq,
            )

        if event.type.startswith("memory."):
            self._project_memory(connection, event, seq)
        if event.type.startswith("evolution."):
            self._project_evolution(connection, event, seq)
        if event.type.startswith("dream.job."):
            self._project_dream(connection, event, seq)

    def _project_agent_runtime(self, connection: sqlite3.Connection, event: Event, seq: int) -> None:
        agent_id = event.payload.get("agent_id")
        if not agent_id:
            return
        state = self._load_projection(connection, "agent", str(agent_id))
        if state is None:
            return
        status_by_type = {
            "runtime.agent.busy": "busy",
            "runtime.agent.idle": "idle",
            "runtime.agent.waiting": "waiting",
            "runtime.agent.degraded": "degraded",
            "runtime.agent.offline": "offline",
            "runtime.agent.crashed": "degraded",
        }
        status = status_by_type.get(event.type)
        if status:
            state["status"] = status
            state["active_run_id"] = (
                event.run_id if status in {"busy", "waiting", "degraded"} else None
            )
            if status in {"idle", "offline"}:
                state["active_assignment_id"] = None
        if event.type == "runtime.agent.crashed":
            state["last_error"] = event.payload.get("reason", "injected crash")
        if event.type == "runtime.agent.heartbeat":
            state["last_heartbeat_at"] = event.created_at.isoformat()
            state["active_assignment_id"] = event.payload.get("assignment_id")
        state["updated_at"] = event.created_at.isoformat()
        self._save_projection(connection, "agent", str(agent_id), state, seq)

    def _project_lease(self, connection: sqlite3.Connection, event: Event, seq: int) -> None:
        assignment_id = event.payload.get("assignment_id")
        if not assignment_id:
            return
        entity_id = str(assignment_id)
        state = self._load_projection(connection, "lease", entity_id) or {
            "assignment_id": entity_id,
            "run_id": event.run_id,
            "task_id": event.task_id,
        }
        state.update(event.payload)
        status_by_type = {
            "assignment.lease.acquired": "active",
            "assignment.lease.renewed": "active",
            "assignment.lease.released": "released",
            "assignment.lease.expired": "expired",
        }
        state["status"] = status_by_type.get(event.type, state.get("status", "active"))
        if event.type == "assignment.lease.acquired":
            state.setdefault("acquired_at", event.created_at.isoformat())
        elif event.type == "assignment.lease.renewed":
            state.setdefault("renewed_at", event.created_at.isoformat())
        elif event.type == "assignment.lease.released":
            state.setdefault("released_at", event.created_at.isoformat())
        elif event.type == "assignment.lease.expired":
            state.setdefault("expired_at", event.created_at.isoformat())
        state["updated_at"] = event.created_at.isoformat()
        self._save_projection(connection, "lease", entity_id, state, seq)

    def _project_mailbox(self, connection: sqlite3.Connection, event: Event, seq: int) -> None:
        agent_id = event.payload.get("mailbox_id")
        if not agent_id:
            return
        state = self._load_projection(connection, "agent", str(agent_id))
        if state is None:
            return
        delta = 1 if event.type.endswith("sent") else -1
        state["mailbox_pending"] = max(0, int(state.get("mailbox_pending", 0)) + delta)
        state["updated_at"] = event.created_at.isoformat()
        self._save_projection(connection, "agent", str(agent_id), state, seq)

    def _project_memory(self, connection: sqlite3.Connection, event: Event, seq: int) -> None:
        candidate_id = event.payload.get("candidate_id")
        if not candidate_id:
            return
        state = self._load_projection(connection, "memory", str(candidate_id)) or {
            "candidate_id": candidate_id,
            "run_id": event.run_id,
            "task_id": event.task_id,
        }
        state.update({key: value for key, value in event.payload.items() if key != "candidate_id"})
        status_by_type = {
            "memory.candidate.proposed": "candidate",
            "memory.review.requested": "review",
            "memory.activated": "active",
            "memory.rejected": "rejected",
            "memory.expired": "expired",
        }
        state["status"] = status_by_type.get(event.type, state.get("status", "candidate"))
        state["updated_at"] = event.created_at.isoformat()
        self._save_projection(connection, "memory", str(candidate_id), state, seq)

    def _project_evolution(self, connection: sqlite3.Connection, event: Event, seq: int) -> None:
        candidate_id = event.payload.get("candidate_id")
        if not candidate_id:
            return
        state = self._load_projection(connection, "evolution", str(candidate_id)) or {
            "candidate_id": candidate_id,
            "run_id": event.run_id,
            "task_id": event.task_id,
        }
        state.update({key: value for key, value in event.payload.items() if key != "candidate_id"})
        status_by_type = {
            "evolution.candidate.proposed": "candidate",
            "evolution.offline.passed": "offline_passed",
            "evolution.shadow.passed": "shadow_passed",
            "evolution.canary.passed": "canary_passed",
            "evolution.promoted": "promoted",
            "evolution.rejected": "rejected",
            "evolution.rolled_back": "rolled_back",
        }
        state["status"] = status_by_type.get(event.type, state.get("status", "candidate"))
        state["updated_at"] = event.created_at.isoformat()
        self._save_projection(connection, "evolution", str(candidate_id), state, seq)

    def _project_dream(self, connection: sqlite3.Connection, event: Event, seq: int) -> None:
        job_id = event.payload.get("job_id")
        if not job_id:
            return
        state = self._load_projection(connection, "dream_job", str(job_id)) or {
            "job_id": job_id,
            "run_id": event.run_id,
            "task_id": event.task_id,
        }
        state.update(event.payload)
        state["status"] = event.type.rsplit(".", 1)[-1]
        state["updated_at"] = event.created_at.isoformat()
        self._save_projection(connection, "dream_job", str(job_id), state, seq)

    @staticmethod
    def _load_projection(
        connection: sqlite3.Connection, kind: str, entity_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT state_json FROM projections WHERE kind = ? AND entity_id = ?",
            (kind, entity_id),
        ).fetchone()
        return json.loads(row["state_json"]) if row else None

    @staticmethod
    def _save_projection(
        connection: sqlite3.Connection,
        kind: str,
        entity_id: str,
        state: dict[str, Any],
        seq: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO projections(kind, entity_id, state_json, updated_seq)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(kind, entity_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_seq = excluded.updated_seq
            """,
            (kind, entity_id, json.dumps(state, ensure_ascii=False), seq),
        )
