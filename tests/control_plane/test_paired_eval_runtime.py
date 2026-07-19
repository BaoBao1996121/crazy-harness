import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from crazy_harness.control_plane.paired_evals import (
    PairedEvalCreationRejected,
    PairedEvalRequest,
)
from crazy_harness.control_plane.runtime import ResidentRuntime
from crazy_harness.core.evals import RecommendationOutcome
from crazy_harness.taskpacks import RepoMaintainerScorer


def test_runtime_runs_a_fair_deterministic_single_vs_team_pair(tmp_path):
    runtime = ResidentRuntime(tmp_path)

    created = runtime.create_paired_eval(
        PairedEvalRequest(
            request_id="runtime-fair-pair-1",
            title="Clamp repair comparison",
            brief="Repair clamp without changing tests.",
            model_mode="scripted",
        )
    )
    runtime.run_until_idle(max_steps=300)
    report = runtime.finalize_paired_eval(created.eval_id)

    assert report.status == "completed"
    assert report.contract.single.input_hash == report.contract.team.input_hash
    assert report.contract.single.model_profile == report.contract.team.model_profile
    assert report.contract.single.model_profile["comparison_semantics"] == (
        "arm_specific_deterministic_scripts"
    )
    assert report.contract.single.model_profile["script_manifest_sha256"] == (
        runtime.team_task_packs["repo-maintainer"].scripted_comparison_manifest_hash()
    )
    assert report.contract.single.model_budget == report.contract.team.model_budget
    assert report.contract.single.workspace != report.contract.team.workspace
    assert report.single.score is not None and report.single.score.passed is True
    assert report.team.score is not None and report.team.score.passed is True
    assert report.single.trace is not None
    assert report.team.trace is not None
    assert report.team.trace.model_completions > report.single.trace.model_completions
    assert report.team.trace.a2a_requests == 1
    assert report.recommendation is not None
    assert report.recommendation.outcome is (
        RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE
    )

    replay = ResidentRuntime(tmp_path).paired_eval(created.eval_id)
    assert replay == report


def test_pair_scorer_fails_closed_when_one_terminal_workspace_is_tampered(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_paired_eval(
        PairedEvalRequest(
            request_id="runtime-tamper-pair-1",
            title="Tamper check",
            brief="Repair clamp without changing tests.",
            model_mode="scripted",
        )
    )
    runtime.run_until_idle(max_steps=300)
    contract = runtime.eval_service.contract(created.eval_id)
    (contract.team.workspace / "tests" / "test_calculator.py").write_text(
        "import unittest\n", encoding="utf-8"
    )

    report = runtime.finalize_paired_eval(created.eval_id)

    assert report.single.score is not None and report.single.score.passed is True
    assert report.team.score is not None and report.team.score.passed is False
    assert report.team.score.checks["tests_unchanged"] is False


def test_live_pair_cancelled_before_first_model_call_persists_invalid_report(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")
    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_paired_eval(
        PairedEvalRequest(
            request_id="runtime-live-cancelled-before-model-1",
            title="Cancelled live pair",
            brief="Do not execute either arm.",
            model_mode="deepseek",
        )
    )
    runtime.cancel_run(created.single_run_id, reason="test_cancelled")
    runtime.cancel_run(created.team_run_id, reason="test_cancelled")

    report = runtime.finalize_paired_eval(created.eval_id)

    assert report.status == "completed"
    assert report.evidence_valid is False
    assert report.invalid_reasons == (
        "single arm has no persisted model call attestation",
        "team arm has no persisted model call attestation",
    )
    assert report.single.status == "cancelled"
    assert report.team.status == "cancelled"
    assert report.recommendation is not None
    assert report.recommendation.outcome is (
        RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE
    )
    assert ResidentRuntime(tmp_path).paired_eval(created.eval_id) == report


def test_runtime_recovers_committed_pair_before_releasing_same_arms(tmp_path):
    runtime = ResidentRuntime(tmp_path)
    request = PairedEvalRequest(
        request_id="runtime-response-loss-1",
        title="Recover committed pair",
        brief="Repair clamp without changing tests.",
        model_mode="scripted",
    )

    def lose_response(point: str) -> None:
        if point == "after_eval_pair_committed":
            raise KeyboardInterrupt("simulated response loss")

    runtime.eval_service.fault_injector = lose_response
    with pytest.raises(KeyboardInterrupt, match="response loss"):
        runtime.create_paired_eval(request)

    committed_records = runtime.store.read_records()
    commit_cursor = next(
        record.cursor
        for record in committed_records
        if record.event.type == "eval.pair.committed"
    )
    assert not any(
        record.event.type == "mailbox.delivery.sent"
        for record in committed_records
    )

    recovered = ResidentRuntime(tmp_path)
    created = recovered.create_paired_eval(request)
    records = recovered.store.read_records()
    release_cursors = [
        record.cursor
        for record in records
        if record.event.type == "mailbox.delivery.sent"
        and record.event.run_id in {created.single_run_id, created.team_run_id}
    ]

    assert len(release_cursors) == 2
    assert all(cursor > commit_cursor for cursor in release_cursors)
    assert sum(
        record.event.type == "eval.pair.created"
        for record in records
    ) == 1


def test_concurrent_create_cannot_persist_both_failed_and_committed(tmp_path):
    first = ResidentRuntime(tmp_path)
    competing = ResidentRuntime(tmp_path)
    request = PairedEvalRequest(
        request_id="runtime-concurrent-create-1",
        title="Create exactly one pair",
        brief="Repair clamp without changing tests.",
        model_mode="scripted",
    )
    first_prepared = threading.Event()
    release_failure = threading.Event()

    def fail_after_single_prepare(point: str) -> None:
        if point == "after_eval_arm_prepared:single":
            first_prepared.set()
            assert release_failure.wait(timeout=10)
            raise ValueError("injected precommit failure")

    first.eval_service.fault_injector = fail_after_single_prepare
    with ThreadPoolExecutor(max_workers=2) as pool:
        failed = pool.submit(first.create_paired_eval, request)
        assert first_prepared.wait(timeout=10)
        contender = pool.submit(competing.create_paired_eval, request)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if any(
                event.type == "eval.pair.committed"
                for event in first.store.read_all()
            ):
                break
            time.sleep(0.01)
        release_failure.set()

        with pytest.raises(PairedEvalCreationRejected):
            failed.result(timeout=10)
        with pytest.raises(PairedEvalCreationRejected):
            contender.result(timeout=10)

    eval_events = [
        event
        for event in first.store.read_all()
        if event.run_id.startswith("eval_")
    ]
    assert sum(event.type == "eval.pair.failed" for event in eval_events) == 1
    assert not any(event.type == "eval.pair.committed" for event in eval_events)


def test_concurrent_finalizers_execute_one_machine_scoring_pass(tmp_path):
    class BlockingCountingScorer(RepoMaintainerScorer):
        def __init__(self) -> None:
            self.calls = 0
            self.lock = threading.Lock()
            self.started = threading.Event()
            self.release = threading.Event()

        def score(self, *args, **kwargs):
            with self.lock:
                self.calls += 1
                current = self.calls
            if current == 1:
                self.started.set()
                assert self.release.wait(timeout=10)
            return super().score(*args, **kwargs)

    runtime = ResidentRuntime(tmp_path)
    created = runtime.create_paired_eval(
        PairedEvalRequest(
            request_id="runtime-concurrent-score-1",
            title="Score once",
            brief="Repair clamp without changing tests.",
            model_mode="scripted",
        )
    )
    runtime.run_until_idle(max_steps=300)
    scorer = BlockingCountingScorer()
    runtime.eval_service.scorer = scorer

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(runtime.finalize_paired_eval, created.eval_id)
        assert scorer.started.wait(timeout=10)
        observer = pool.submit(runtime.finalize_paired_eval, created.eval_id)
        observed = observer.result(timeout=10)
        scorer.release.set()
        completed = winner.result(timeout=30)

    assert observed.status == "running"
    assert completed.status == "completed"
    assert scorer.calls == 2
    assert runtime.finalize_paired_eval(created.eval_id) == completed
