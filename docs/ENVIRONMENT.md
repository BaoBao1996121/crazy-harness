# Environment Checklist

> Purpose: record external runtime dependencies and verified availability before code changes. Never place credentials in this file.

## Required for v0.10 Trusted Looping MVP

| Dependency | Required version or contract | Current verification | Status |
|---|---|---|---|
| Python | `>=3.11`; current local interpreter is used by the existing test suite | Existing EL0/EL1 tests and repository suite execute on Python 3.13 | Available |
| SQLite | Python standard-library `sqlite3`; durable EventStore and work claims | EL1 service tests and engineering-loop replay Spikes passed | Available |
| Pydantic | `>=2.7` from `pyproject.toml` | Existing domain models import and validate in tests | Available |
| FastAPI / Uvicorn | Existing project dependencies; used by AgentRun and Engineering Loop control APIs | Parent Loop 的 Create/List/Get/Advance/Drain/Pause/Resume/Cancel、SSE 和 health `v0.10.0-dev` verified locally | Available |
| React / TypeScript toolchain | Existing `frontend/` lockfile; used by Control Room | Full Vitest `24 files / 91 tests passed` and production build passed | Available |

## Optional or externally gated

| Dependency | Purpose | Current status | v0.10 policy |
|---|---|---|---|
| DeepSeek API | Live-model Engineering Loop evidence | `DEEPSEEK_API_KEY` is not configured | Not required for deterministic EL2; keep Live evidence explicitly unverified |
| Docker Engine | Strong tool isolation | Docker CLI/Engine is unavailable on this host | Not required for disposable local Repo Golden; do not claim sandbox isolation |
| Browser / Chromium | Control Room visual verification | Engineering Loop completed/paused/child-drilldown views verified at 1440x900 and 390x844 with no console warnings/errors or horizontal overflow | Available for local verification |
| Remote A2A / broker | Cross-machine Agent workers | Not configured | Out of v0.10 scope |
| RDKit / GPU / HPC | Scientific Harness vertical | Not installed or verified for this milestone | Out of v0.10 scope |

## Verified design assumptions

The reproducible records are the six `work/spikes/verify_engineering_loop_*.py` scripts and the matching entries in `docs/DEVELOPMENT_JOURNAL.md`:

1. `loop_id + iteration` deterministically derives unique child identities.
2. `WorkspaceSnapshotStore` preserves immutable Active-to-Candidate lineage.
3. Replayed deterministic parent events do not create duplicate formal facts.
4. Parent-scoped SSE is read-only and does not leak peer Loop events.
5. Targeted advance/drain cannot progress an unrelated parent Loop.
6. Iteration child identities resolve to canonical AgentRun projections for UI drilldown.

All three Spikes passed before EL2 implementation. External model quality, Docker isolation, throughput, and production multi-process behavior remain separate gates.
