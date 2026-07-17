from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile
from typing import Any


from crazy_harness.core.evals.evolution import (
    ChangeTarget,
    EvolutionCandidate,
    EvolutionController,
    EvolutionStatus,
    TypedDiff,
)
from crazy_harness.core.evals.models import (
    EvalScenario,
    MetricDirection,
    MetricThreshold,
    ScenarioMetrics,
)
from crazy_harness.core.evals.runner import EvalRunner
from crazy_harness.core.memory.models import MemoryCandidate, MemorySlot, MemoryStatus
from crazy_harness.core.memory.store import MemoryConflictError, MemoryStore


def _relative(path: Path, output: Path) -> str:
    return path.resolve().relative_to(output.resolve()).as_posix()


def _memory_summary(candidate: MemoryCandidate) -> dict[str, str]:
    return {
        "candidate_id": candidate.candidate_id,
        "status": candidate.status.value,
        "reviewed_by": candidate.reviewed_by or "",
        "reason": candidate.decision_reason,
    }


def _candidate(
    slot: MemorySlot,
    content: str,
    scope: str,
    evidence: str,
    version: int = 1,
) -> MemoryCandidate:
    return MemoryCandidate(
        slot=slot, content=content, scope=scope, evidence=[evidence], version=version
    )


def _metrics(task_success: float, token_cost: float) -> ScenarioMetrics:
    return ScenarioMetrics(
        scenario_id="safe-release",
        metrics={"task_success": task_success, "token_cost": token_cost},
    )


def run_scenario(output: Path | None = None) -> dict[str, Any]:
    output = output.resolve() if output else Path(tempfile.mkdtemp(prefix="crazy-block-08-"))
    output.mkdir(parents=True, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="runtime_", dir=output))
    memory_path = runtime / "memory.jsonl"
    memory_store = MemoryStore(memory_path)

    approved = memory_store.propose(
        _candidate(
            MemorySlot.USER_CONSTRAINT,
            "Never deploy directly to production.",
            "project:crazy",
            "event://block-08/human-correction",
        )
    )
    approved = memory_store.approve(
        approved.candidate_id,
        reviewer="course-owner",
        reason="Direct human instruction.",
    )
    rejected = memory_store.propose(
        _candidate(
            MemorySlot.WORLD_FACT,
            "An unverified staging endpoint exists.",
            "world:staging",
            "event://block-08/model-claim",
        )
    )
    rejected = memory_store.reject(
        rejected.candidate_id,
        reviewer="course-owner",
        reason="Evidence is not authoritative.",
    )
    current = memory_store.propose(
        _candidate(
            MemorySlot.PREFERENCE,
            "Use concise release notes.",
            "user:owner",
            "event://block-08/review-1",
        )
    )
    current = memory_store.approve(current.candidate_id, reviewer="course-owner")
    conflicting = memory_store.propose(
        _candidate(
            MemorySlot.PREFERENCE,
            "Always use verbose release notes.",
            "user:owner",
            "event://block-08/review-2",
            2,
        )
    )
    conflict_ids: list[str] = []
    try:
        memory_store.approve(conflicting.candidate_id, reviewer="course-owner")
    except MemoryConflictError as error:
        conflict_ids = error.conflicting_ids
    conflict_status = memory_store.get(conflicting.candidate_id).status
    recalled = MemoryStore(memory_path).recall(scope="project:crazy")

    eval_scenario = EvalScenario(
        scenario_id="safe-release",
        description="A cheaper candidate must not regress release success.",
        metrics=[
            MetricThreshold(
                name="task_success",
                direction=MetricDirection.AT_LEAST,
                threshold=0.80,
                max_regression=0.02,
            ),
            MetricThreshold(
                name="token_cost",
                direction=MetricDirection.AT_MOST,
                threshold=1000,
            ),
        ],
    )
    report = EvalRunner().compare(
        scenarios=[eval_scenario],
        baseline=[_metrics(0.95, 900)],
        candidate=[_metrics(0.86, 600)],
        baseline_version="v1",
        candidate_version="v2-bad",
    )
    metric_fields = {
        "name",
        "baseline",
        "candidate",
        "favorable_delta",
        "threshold_met",
        "non_regression_met",
        "passed",
    }
    metric_summaries = [
        metric.model_dump(include=metric_fields, mode="json")
        for metric in report.scenarios[0].metrics
    ]
    comparisons = {metric.name: metric for metric in report.scenarios[0].metrics}

    evolution_path = runtime / "evolution.jsonl"
    evolution = EvolutionController(evolution_path, initial_version="v1")
    bad_candidate = evolution.submit(
        EvolutionCandidate(
            base_version="v1",
            proposed_version="v2-bad",
            scope="project:crazy",
            rationale="Use fewer tokens by removing verification instructions.",
            evidence=["eval://safe-release/v2-bad"],
            diffs=[
                TypedDiff(
                    target=ChangeTarget.PROMPT,
                    path="prompt.release.instructions",
                    before="verify, then summarize",
                    after="summarize",
                )
            ],
        )
    )
    bad_candidate = evolution.offline_gate(bad_candidate.candidate_id, report)
    reopened_evolution = EvolutionController(evolution_path, initial_version="v1")

    checks = {
        "memory_approved_and_recalled": approved.status is MemoryStatus.APPROVED and recalled == [approved],
        "memory_rejected_and_not_recalled": rejected.status is MemoryStatus.REJECTED and memory_store.recall(scope="world:staging") == [],
        "memory_conflict_fail_closed": conflict_ids == [current.candidate_id] and conflict_status is MemoryStatus.CANDIDATE,
        "baseline_passes": report.baseline_passed,
        "bad_candidate_fails_non_regression": not report.passed and not comparisons["task_success"].non_regression_met,
        "cheaper_metric_still_passes": comparisons["token_cost"].passed,
        "evolution_bad_candidate_rejected": bad_candidate.status is EvolutionStatus.REJECTED and bad_candidate.rejection_reason == "offline_eval_failed",
        "active_version_unchanged": reopened_evolution.active_version == "v1",
    }
    evidence = {
        "scenario": "block_08_eval_memory_evolution",
        "result": "pass" if all(checks.values()) else "fail",
        "memory": {
            "store_path": _relative(memory_path, output),
            "approved": _memory_summary(approved),
            "rejected": _memory_summary(rejected),
            "conflict": {
                "candidate_id": conflicting.candidate_id,
                "detected": bool(conflict_ids),
                "conflicting_ids": conflict_ids,
                "candidate_status_after_conflict": conflict_status.value,
            },
            "recalled_candidate_ids": [candidate.candidate_id for candidate in recalled],
        },
        "eval": {
            "baseline_version": report.baseline_version,
            "bad_candidate_version": report.candidate_version,
            "baseline_passed": report.baseline_passed,
            "bad_candidate_passed": report.passed,
            "metrics": metric_summaries,
        },
        "evolution": {
            "store_path": _relative(evolution_path, output),
            "candidate_id": bad_candidate.candidate_id,
            "baseline_version": bad_candidate.base_version,
            "proposed_version": bad_candidate.proposed_version,
            "status": bad_candidate.status.value,
            "rejection_reason": bad_candidate.rejection_reason,
            "active_version": reopened_evolution.active_version,
        },
        "checks": checks,
    }
    if not all(checks.values()):
        raise RuntimeError("Block 8 scenario evidence checks failed")

    json_path = output / "evidence.json"
    markdown_path = output / "evidence.md"
    json_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(
        "# Block 8 Memory, Eval, and Evolution Evidence\n\n"
        "- Result: **PASS**\n"
        f"- Memory: approved `{approved.candidate_id}`, rejected `{rejected.candidate_id}`, conflict blocked `{conflicting.candidate_id}`\n"
        f"- Eval: baseline passed `{report.baseline_passed}`, bad candidate passed `{report.passed}`\n"
        f"- Evolution: `{bad_candidate.status.value}` (`{bad_candidate.rejection_reason}`), active version `{reopened_evolution.active_version}`\n"
        f"- Raw runtime: `{runtime.name}/`\n",
        encoding="utf-8",
    )
    print(f"evidence_json={json_path}")
    print(f"evidence_markdown={markdown_path}")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Block 8 memory/eval/evolution scenario.")
    parser.add_argument("--output", type=Path, help="Evidence directory; defaults to a temporary directory.")
    args = parser.parse_args()
    run_scenario(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
