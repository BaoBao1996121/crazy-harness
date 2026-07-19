import pytest

from crazy_harness.control_plane.paired_evals import (
    EvalRunIdentity,
    PairedEvalCreationRejected,
    PairedEvalRequest,
    PairedEvalService,
    paired_input_hash,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event
from crazy_harness.core.evals import PairedEvalContract
from crazy_harness.taskpacks import RepoMaintainerScorer, RepoMaintainerTaskPack


def test_pair_creation_cancels_both_arms_when_persisted_budgets_differ(tmp_path):
    store = SQLiteEventStore(tmp_path / "eval.db")
    service = PairedEvalService(store)
    request = PairedEvalRequest(
        request_id="budget-mismatch-1",
        title="Fair pair",
        brief="Repair the fixture.",
    )
    pack = RepoMaintainerTaskPack(tmp_path)
    cancelled: list[str] = []

    def submit(mode: str, identity: EvalRunIdentity) -> EvalRunIdentity:
        run_id = identity.run_id
        prepared = pack.prepare(run_id)
        metadata = pack.case_metadata(prepared)
        budget = request.model_budget.model_dump(mode="json")
        if mode == "team":
            budget["max_total_tokens"] += 1
        store.append(
            Event(
                run_id=run_id,
                task_id=identity.task_id,
                type="run.created",
                source="test",
                payload={
                    "title": "Fair pair",
                    "brief": "Repair the fixture.",
                    "execution_mode": mode,
                    "task_pack": "repo-maintainer",
                    "model_mode": "scripted",
                    "model_budget": budget,
                    "model_profile": {
                        "provider": "FakeModelProvider",
                        "model": "repo-maintainer-scripted-v1",
                        "deterministic": True,
                    },
                    "case_id": "clamp-bounds-v1",
                    "input_hash": paired_input_hash(
                        request, metadata["fixture_hash"]
                    ),
                    "workspace_path": str(prepared.workspace),
                    "baseline_path": str(prepared.baseline),
                    **metadata,
                },
            )
        )
        return identity

    with pytest.raises(PairedEvalCreationRejected, match="different model_budget"):
        service.create(
            request,
            prepare_arm=submit,
            release_arm=lambda _mode, _identity: None,
            cancel_arm=lambda run_id: cancelled.append(run_id),
        )

    assert len(cancelled) == 2
    assert all(run_id.startswith("run_") for run_id in cancelled)
    assert len(set(cancelled)) == 2
    failed = [event for event in store.read_all() if event.type == "eval.pair.failed"]
    assert len(failed) == 1


def test_report_rejects_scorer_version_drift_after_runtime_upgrade(tmp_path):
    class UpgradedScorer(RepoMaintainerScorer):
        scorer_version = "repo-maintainer-v3"

    store = SQLiteEventStore(tmp_path / "eval.db")
    service = PairedEvalService(store)
    request = PairedEvalRequest(
        request_id="scorer-upgrade-1",
        title="Fair pair",
        brief="Repair the fixture.",
    )
    pack = RepoMaintainerTaskPack(tmp_path)

    identities: dict[str, EvalRunIdentity] = {}

    def submit(mode: str, identity: EvalRunIdentity) -> EvalRunIdentity:
        identities[mode] = identity
        run_id = identity.run_id
        task_id = identity.task_id
        prepared = pack.prepare(run_id)
        metadata = pack.case_metadata(prepared)
        (prepared.workspace / "calculator.py").write_text(
            pack.fixed_source(), encoding="utf-8"
        )
        store.append(
            Event(
                run_id=run_id,
                task_id=task_id,
                type="run.created",
                source="test",
                payload={
                    "title": request.title,
                    "brief": request.brief,
                    "execution_mode": mode,
                    "task_pack": "repo-maintainer",
                    "model_mode": "scripted",
                    "model_budget": request.model_budget.model_dump(mode="json"),
                    "model_profile": {
                        "provider": "FakeModelProvider",
                        "model": "repo-maintainer-scripted-v1",
                        "deterministic": True,
                    },
                    "workspace_path": str(prepared.workspace),
                    "baseline_path": str(prepared.baseline),
                    "input_hash": paired_input_hash(
                        request, metadata["fixture_hash"]
                    ),
                    **metadata,
                },
            )
        )
        return identity

    created = service.create(
        request,
        prepare_arm=submit,
        release_arm=lambda _mode, _identity: None,
    )
    for mode, run_id in (
        ("single", created.single_run_id),
        ("team", created.team_run_id),
    ):
        store.append(
            Event(
                run_id=run_id,
                task_id=identities[mode].task_id,
                type="run.succeeded",
                source="test",
            )
        )

    upgraded_service = PairedEvalService(store, scorer=UpgradedScorer())
    assert upgraded_service.finalize_ready() == 0
    failures = [
        event
        for event in store.read_all(run_id=created.eval_id)
        if event.type == "eval.pair.finalization.failed"
    ]
    assert len(failures) == 1
    assert upgraded_service.finalize_ready() == 0
    assert sum(
        event.type == "eval.pair.finalization.failed"
        for event in store.read_all(run_id=created.eval_id)
    ) == 1
    with pytest.raises(RuntimeError, match="scorer version"):
        upgraded_service.finalize(created.eval_id)


def test_committed_pair_resumes_with_the_same_arms_after_response_loss(tmp_path):
    store = SQLiteEventStore(tmp_path / "eval.db")
    pack = RepoMaintainerTaskPack(tmp_path)
    request = PairedEvalRequest(
        request_id="response-loss-1",
        title="Fair pair",
        brief="Repair the exact same fixture.",
    )
    prepared_calls: list[tuple[str, str]] = []
    released: list[tuple[str, str]] = []

    def prepare(mode: str, identity: EvalRunIdentity) -> EvalRunIdentity:
        prepared_calls.append((mode, identity.run_id))
        prepared = pack.prepare(identity.run_id)
        metadata = pack.case_metadata(prepared)
        store.append(
            Event(
                id=f"created-{mode}",
                run_id=identity.run_id,
                task_id=identity.task_id,
                type="run.created",
                source="test",
                payload={
                    "title": request.title,
                    "brief": request.brief,
                    "execution_mode": mode,
                    "task_pack": "repo-maintainer",
                    "model_mode": "scripted",
                    "model_budget": request.model_budget.model_dump(mode="json"),
                    "model_profile": {
                        "provider": "FakeModelProvider",
                        "model": "repo-maintainer-scripted-v1",
                        "deterministic": True,
                    },
                    "workspace_path": str(prepared.workspace),
                    "baseline_path": str(prepared.baseline),
                    "input_hash": paired_input_hash(
                        request, metadata["fixture_hash"]
                    ),
                    **metadata,
                },
            )
        )
        return identity

    def lose_response(point: str) -> None:
        if point == "after_eval_pair_committed":
            raise KeyboardInterrupt("simulated response loss")

    interrupted = PairedEvalService(store, fault_injector=lose_response)
    with pytest.raises(KeyboardInterrupt, match="response loss"):
        interrupted.create(
            request,
            prepare_arm=prepare,
            release_arm=lambda mode, identity: released.append(
                (mode, identity.run_id)
            ),
        )

    assert released == []
    assert any(
        event.type == "eval.pair.committed"
        for event in store.read_all()
    )

    recovered = PairedEvalService(store).create(
        request,
        prepare_arm=prepare,
        release_arm=lambda mode, identity: released.append(
            (mode, identity.run_id)
        ),
    )

    assert prepared_calls == [
        ("single", recovered.single_run_id),
        ("team", recovered.team_run_id),
    ]
    assert released == [
        ("single", recovered.single_run_id),
        ("team", recovered.team_run_id),
    ]
    eval_events = store.read_all(run_id=recovered.eval_id)
    assert sum(event.type == "eval.pair.created" for event in eval_events) == 1
    assert sum(event.type == "eval.pair.committed" for event in eval_events) == 1
    assert sum(event.type == "eval.arm.released" for event in eval_events) == 2
    for run_id in (recovered.single_run_id, recovered.team_run_id):
        assert sum(
            event.type == "eval.arm.linked"
            for event in store.read_all(run_id=run_id)
        ) == 1
    with pytest.raises(ValueError, match="idempotency"):
        PairedEvalService(store).create(
            request.model_copy(update={"brief": "Different work"}),
            prepare_arm=prepare,
            release_arm=lambda _mode, _identity: None,
        )


def test_recovery_prepare_error_keeps_pair_retryable(tmp_path):
    store = SQLiteEventStore(tmp_path / "eval.db")
    service = PairedEvalService(store)
    request = PairedEvalRequest(
        request_id="recovery-config-gap-1",
        title="Recover after config repair",
        brief="Repair the exact same fixture.",
    )

    with pytest.raises(ValueError, match="temporary config gap"):
        service.create(
            request,
            prepare_arm=lambda _mode, _identity: (_ for _ in ()).throw(
                ValueError("temporary config gap")
            ),
            release_arm=lambda _mode, _identity: None,
            fail_precommit=False,
        )

    assert not any(
        event.type == "eval.pair.failed" for event in store.read_all()
    )
    assert any(
        event.type == "eval.pair.requested" for event in store.read_all()
    )


def test_live_attestation_rejects_same_model_name_with_different_inference_profile(
    tmp_path,
):
    store = SQLiteEventStore(tmp_path / "eval.db")
    expected_profile = {
        "provider": "DeepSeekOpenAIProvider",
        "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com",
        "thinking_mode": "disabled",
        "sampling_control": "provider_default_unseeded",
        "max_output_tokens": 4096,
        "timeout_seconds": 60.0,
    }
    contract = PairedEvalContract.model_validate(
        {
            "eval_id": "eval-attestation",
            "case_id": "clamp-bounds-v1",
            "task_pack": "repo-maintainer",
            "fixture_hash": "f" * 64,
            "scorer_version": "repo-maintainer-v2",
            "evidence_tier": "live_paired",
            "single": {
                "execution_mode": "single",
                "run_id": "run-single",
                "workspace": tmp_path / "single",
                "input_hash": "i" * 64,
                "model_profile": expected_profile,
                "model_budget": {"max_total_tokens": 10000},
            },
            "team": {
                "execution_mode": "team",
                "run_id": "run-team",
                "workspace": tmp_path / "team",
                "input_hash": "i" * 64,
                "model_profile": expected_profile,
                "model_budget": {"max_total_tokens": 10000},
            },
        }
    )
    actual_profile = {**expected_profile, "max_output_tokens": 2048}
    for mode, run_id in (("single", "run-single"), ("team", "run-team")):
        store.append(
            Event(
                run_id=run_id,
                task_id=f"task-{mode}",
                type="run.created",
                source="test",
                payload={
                    "model_mode": "deepseek",
                    "model_budget": {
                        "max_total_tokens": 10000,
                        "max_cost_usd": "1.0",
                        "max_concurrent_calls": 2,
                        "max_output_tokens_per_call": 4096,
                        "max_retries_per_call": 2,
                    },
                },
            )
        )
        call_id = f"call-{mode}"
        store.reserve_model_call(
            call_id=call_id,
            run_id=run_id,
            task_id=f"task-{mode}",
            agent_id=mode,
            provider="DeepSeekOpenAIProvider",
            model="deepseek-v4-flash",
            reserved_input_tokens=10,
            reserved_output_tokens=20,
            reserved_cost_microusd=1,
            max_total_tokens=10000,
            max_cost_microusd=1_000_000,
            max_concurrent_calls=2,
        )
        store.append(
            Event(
                run_id=run_id,
                task_id=f"task-{mode}",
                type="model.call.reserved",
                source="runtime.model-governance",
                payload={
                    "call_id": call_id,
                    "provider_profile": actual_profile,
                },
            )
        )

    with pytest.raises(ValueError, match="profile"):
        PairedEvalService(store)._validate_live_model_attestation(contract)
