from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from crazy_harness.control_plane.engineering_loops import ChildRunOutcome
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.checkpoints import WorkspaceSnapshotStore
from crazy_harness.core.engineering_loops import (
    CandidateProposal,
    EngineeringIterationIdentity,
    EngineeringLoopContract,
    IterationEvaluation,
)
from crazy_harness.core.runtime.local import GuardedLocalRuntime, LocalRuntimeTimeout
from crazy_harness.taskpacks import RepoQualityTaskPack


class RepoQualityLoopPack:
    """Golden LoopPack: improve behavior, then remove a tracked quality marker."""

    loop_pack_id = "repo-quality"
    required_permissions = frozenset(
        {"disposable_workspace_write", "run_bounded_checks"}
    )

    def __init__(
        self,
        data_dir: Path,
        *,
        task_pack: RepoQualityTaskPack,
        snapshots: WorkspaceSnapshotStore,
        artifacts: ArtifactStore,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.task_pack = task_pack
        self.snapshots = snapshots
        self.artifacts = artifacts

    def propose(
        self,
        contract: EngineeringLoopContract,
        identity: EngineeringIterationIdentity,
        base_state_ref: str,
    ) -> CandidateProposal:
        del contract
        if identity.iteration == 1:
            change_set = {
                "kind": "repository_edit",
                "path": "calculator.py",
                "intent": "repair_clamp_behavior",
            }
            rationale = "The frozen baseline fails the bounded behavior tests."
            expected_effect = "All unit tests pass while the quality marker remains visible."
        else:
            change_set = {
                "kind": "repository_edit",
                "path": "calculator.py",
                "intent": "remove_verified_quality_marker",
            }
            rationale = "Behavior is verified; the temporary quality marker is now removable."
            expected_effect = "Tests remain green and the QUALITY-TODO marker disappears."
        return CandidateProposal(
            candidate_id=identity.candidate_id,
            iteration=identity.iteration,
            base_state_ref=base_state_ref,
            change_set=change_set,
            rationale=rationale,
            expected_effect=expected_effect,
            proposer_attestation={
                "kind": "deterministic_golden_loop",
                "version": "repo-quality-v1",
            },
        )

    def evaluate(
        self,
        contract: EngineeringLoopContract,
        candidate: CandidateProposal,
        outcome: ChildRunOutcome,
    ) -> IterationEvaluation:
        """Re-run checks from the immutable candidate snapshot, not Agent claims."""

        if not outcome.candidate_state_ref.startswith("snapshot://"):
            return self._invalid_evaluation(
                contract,
                candidate,
                outcome,
                "child outcome has no immutable workspace snapshot",
            )
        object_id = outcome.candidate_state_ref.removeprefix("snapshot://")
        self.snapshots.verify(object_id)
        evaluation_workspace = (
            self.data_dir
            / "engineering_evaluations"
            / contract.loop_id
            / candidate.candidate_id
        )
        if evaluation_workspace.exists():
            restored = self.snapshots.create(evaluation_workspace)
            if restored.object_id != object_id:
                raise RuntimeError("persisted evaluator workspace does not match candidate")
        else:
            self.snapshots.restore(object_id, evaluation_workspace)

        # Read the frozen source before spawning the verifier. Some enterprise
        # endpoint-protection products transform executed source files at rest.
        source = (evaluation_workspace / "calculator.py").read_text(encoding="utf-8")

        command = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ]
        timed_out = False
        runtime = GuardedLocalRuntime(
            evaluation_workspace,
            allowed_commands={Path(sys.executable).name},
            max_timeout_seconds=30,
        )
        try:
            completed = runtime.run(
                command,
                timeout_seconds=30,
            )
            return_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except LocalRuntimeTimeout as exc:
            timed_out = True
            return_code = -1
            stdout = ""
            stderr = str(exc)

        behavior_fixed = "return max(lower, min(value, upper))" in source
        marker_removed = "QUALITY-TODO" not in source
        tests_passed = return_code == 0 and not timed_out
        score = Decimal("0")
        if tests_passed and behavior_fixed:
            score += Decimal("0.5")
        if tests_passed and behavior_fixed and marker_removed:
            score += Decimal("0.5")

        evidence = self.artifacts.write_json(
            "engineering_evaluation",
            {
                "loop_id": contract.loop_id,
                "candidate_id": candidate.candidate_id,
                "iteration": candidate.iteration,
                "candidate_state_ref": outcome.candidate_state_ref,
                "command": command,
                "return_code": return_code,
                "timed_out": timed_out,
                "stdout": stdout,
                "stderr": stderr,
                "checks": {
                    "child_succeeded": outcome.status == "succeeded",
                    "tests_passed": tests_passed,
                    "behavior_fixed": behavior_fixed,
                    "quality_marker_removed": marker_removed,
                },
                "quality_score": str(score),
            },
            summary=(
                f"Independent repo-quality evaluation for iteration {candidate.iteration}"
            ),
        )
        return IterationEvaluation(
            iteration=candidate.iteration,
            candidate_id=candidate.candidate_id,
            candidate_state_ref=outcome.candidate_state_ref,
            evaluator_version=contract.metric.evaluator_version,
            metrics={contract.metric.name: score},
            hard_gates={
                "child_succeeded": outcome.status == "succeeded",
                "tests_passed": tests_passed,
                "snapshot_verified": True,
            },
            evidence_refs=(evidence.uri, outcome.candidate_state_ref),
            valid=True,
        )

    @staticmethod
    def _invalid_evaluation(
        contract: EngineeringLoopContract,
        candidate: CandidateProposal,
        outcome: ChildRunOutcome,
        reason: str,
    ) -> IterationEvaluation:
        return IterationEvaluation(
            iteration=candidate.iteration,
            candidate_id=candidate.candidate_id,
            candidate_state_ref=outcome.candidate_state_ref,
            evaluator_version=contract.metric.evaluator_version,
            metrics={contract.metric.name: Decimal("0")},
            hard_gates={"immutable_snapshot": False},
            evidence_refs=outcome.evidence_refs,
            valid=False,
            invalid_reasons=(reason,),
        )
