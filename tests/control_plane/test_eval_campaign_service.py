import sqlite3
from pathlib import Path

import pytest

from crazy_harness.control_plane.eval_campaigns import (
    EvalCampaignRequest,
    EvalCampaignService,
)
from crazy_harness.control_plane.paired_evals import (
    PairedEvalArmReport,
    PairedEvalCreated,
    PairedEvalReport,
    paired_eval_id,
)
from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.evals import EvidenceTier, PairedEvalArm, PairedEvalContract
from crazy_harness.core.evals import RecommendationOutcome, RunTraceMetrics
from crazy_harness.core.events import Event
from crazy_harness.taskpacks import RepoMaintainerScore


def _pair_contract(
    *,
    eval_id: str,
    root: Path,
    model_budget: dict[str, object],
) -> PairedEvalContract:
    shared = {
        "input_hash": "same-input",
        "model_profile": {"provider": "FakeModelProvider", "model": "suite-v1"},
        "model_budget": model_budget,
    }
    return PairedEvalContract(
        eval_id=eval_id,
        case_id="clamp-bounds-v1",
        task_pack="repo-maintainer",
        fixture_hash="fixture-v1",
        scorer_version="repo-maintainer-v2",
        evidence_tier=EvidenceTier.DETERMINISTIC,
        harness_profile={
            "single_behavior_version": "single-v1",
            "team_behavior_version": "team-v1",
            "supervisor_policy": "supervisor-v1",
            "team_contract": {"stages": ["inspect", "build", "review"]},
        },
        single=PairedEvalArm(
            execution_mode="single",
            run_id=f"run_{eval_id}_single",
            workspace=root / f"{eval_id}-single",
            **shared,
        ),
        team=PairedEvalArm(
            execution_mode="team",
            run_id=f"run_{eval_id}_team",
            workspace=root / f"{eval_id}-team",
            **shared,
        ),
    )


def _completed_pair_report(contract: PairedEvalContract) -> PairedEvalReport:
    score = RepoMaintainerScore(
        passed=True,
        score=1.0,
        checks={"tests_passed": True},
        changed_files=("calculator.py",),
    )

    def trace(run_id: str, event_id: str, duration_ms: int) -> RunTraceMetrics:
        return RunTraceMetrics(
            run_id=run_id,
            terminal_status="succeeded",
            terminal_event_id=event_id,
            duration_ms=duration_ms,
            model_requests=1,
            model_completions=1,
            physical_model_attempts=0,
            tool_requests=1,
            tool_completions=1,
            operations_started=1,
            operations_completed=1,
            a2a_requests=0,
            a2a_responses=0,
            assignment_failures=0,
            assignment_retries=0,
            operation_unknowns=0,
            model_unknown_calls=0,
            dead_letters=0,
            spent_tokens=0,
            committed_tokens=0,
            spent_cost_microusd=0,
            committed_cost_microusd=0,
        )

    return PairedEvalReport(
        eval_id=contract.eval_id,
        status="completed",
        contract=contract,
        single=PairedEvalArmReport(
            execution_mode="single",
            run_id=contract.single.run_id,
            status="succeeded",
            score=score,
            trace=trace(
                contract.single.run_id, f"terminal-{contract.eval_id}-single", 10
            ),
        ),
        team=PairedEvalArmReport(
            execution_mode="team",
            run_id=contract.team.run_id,
            status="succeeded",
            score=score,
            trace=trace(contract.team.run_id, f"terminal-{contract.eval_id}-team", 12),
        ),
        recommendation=None,
    )


def _record_pair_commit(
    store: SQLiteEventStore,
    contract: PairedEvalContract,
) -> None:
    store.append(
        Event(
            id=f"pair-committed-{contract.eval_id}",
            run_id=contract.eval_id,
            task_id=contract.eval_id,
            type="eval.pair.committed",
            source="runtime.eval",
            payload={
                "single_run_id": contract.single.run_id,
                "team_run_id": contract.team.run_id,
            },
        )
    )


def test_campaign_create_freezes_parent_plan_without_starting_child_pairs(tmp_path):
    store = SQLiteEventStore(tmp_path / "campaign.db")
    service = EvalCampaignService(store)
    request = EvalCampaignRequest(
        request_id="campaign-create-1",
        title="Repeat the same repair",
        brief="Compare Single and Team over three pre-registered trials.",
        trial_count=3,
    )

    created = service.create(request)
    replay = service.create(request)

    assert replay == created
    assert created.planned_trial_count == 3
    contract = service.contract(created.campaign_id)
    assert contract.trials[0].trial_index == 1
    assert contract.pair_model_budget == request.model_budget.model_dump(mode="json")
    assert not any(event.type == "eval.pair.requested" for event in store.read_all())
    assert sum(event.type == "eval.campaign.created" for event in store.read_all()) == 1
    with pytest.raises(ValueError, match="idempotency key"):
        service.create(request.model_copy(update={"brief": "different input"}))


def test_advance_persists_parent_link_before_releasing_child_pair(tmp_path):
    store = SQLiteEventStore(tmp_path / "campaign.db")
    service = EvalCampaignService(store)
    request = EvalCampaignRequest(
        request_id="campaign-advance-1",
        title="Repeat the same repair",
        brief="Prove parent-before-child release ordering.",
        trial_count=2,
        max_parallel_pairs=1,
    )
    created = service.create(request)
    contracts: dict[str, PairedEvalContract] = {}
    actions: list[tuple[str, object]] = []

    def create_pair(child_request):
        actions.append(("create", child_request.parent_trial_index))
        assert child_request.release_policy == "campaign_linked"
        assert child_request.parent_campaign_id == created.campaign_id
        eval_id = paired_eval_id(child_request.request_id)
        contract = _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=child_request.model_budget.model_dump(mode="json"),
        )
        contracts[eval_id] = contract
        _record_pair_commit(store, contract)
        return PairedEvalCreated(
            eval_id=eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    def release_pair(eval_id: str):
        assert any(
            event.type == "eval.campaign.trial.linked"
            and event.payload["eval_id"] == eval_id
            for event in store.read_all(run_id=created.campaign_id)
        )
        actions.append(("release", eval_id))

    def pair_report(eval_id: str) -> PairedEvalReport:
        contract = contracts[eval_id]
        return PairedEvalReport(
            eval_id=eval_id,
            status="running",
            contract=contract,
            single=PairedEvalArmReport(
                execution_mode="single",
                run_id=contract.single.run_id,
                status="queued",
            ),
            team=PairedEvalArmReport(
                execution_mode="team",
                run_id=contract.team.run_id,
                status="queued",
            ),
        )

    assert service.advance_one(
        created.campaign_id,
        create_pair=create_pair,
        pair_contract=contracts.__getitem__,
        release_pair=release_pair,
        pair_report=pair_report,
    )
    assert actions == []
    assert any(
        event.type == "eval.campaign.trial.started"
        and event.payload["trial_index"] == 1
        for event in store.read_all(run_id=created.campaign_id)
    )

    assert service.advance_one(
        created.campaign_id,
        create_pair=create_pair,
        pair_contract=contracts.__getitem__,
        release_pair=release_pair,
        pair_report=pair_report,
    )
    assert actions == [("create", 1)]
    first_eval_id = next(iter(contracts))

    assert service.advance_one(
        created.campaign_id,
        create_pair=create_pair,
        pair_contract=contracts.__getitem__,
        release_pair=release_pair,
        pair_report=pair_report,
    )
    assert actions == [("create", 1), ("release", first_eval_id)]

    # Trial 1 remains active, so a replayed service cannot exceed the one-Pair window.
    replay = EvalCampaignService(SQLiteEventStore(tmp_path / "campaign.db"))
    assert not replay.advance_one(
        created.campaign_id,
        create_pair=create_pair,
        pair_contract=lambda eval_id: _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=request.model_budget.model_dump(mode="json"),
        ),
        release_pair=release_pair,
        pair_report=lambda eval_id: PairedEvalReport(
            eval_id=eval_id,
            status="running",
            contract=_pair_contract(
                eval_id=eval_id,
                root=tmp_path,
                model_budget=request.model_budget.model_dump(mode="json"),
            ),
            single=PairedEvalArmReport(
                execution_mode="single",
                run_id=f"run_{eval_id}_single",
                status="running",
            ),
            team=PairedEvalArmReport(
                execution_mode="team",
                run_id=f"run_{eval_id}_team",
                status="running",
            ),
        ),
    )
    assert [item for item in actions if item[0] == "create"] == [("create", 1)]


def test_cancel_is_persistent_idempotent_and_blocks_future_trial_effects(tmp_path):
    store = SQLiteEventStore(tmp_path / "campaign.db")
    service = EvalCampaignService(store)
    created = service.create(
        EvalCampaignRequest(
            request_id="campaign-cancel-1",
            title="Cancel a partially prepared campaign",
            brief="Do not create or release another Pair after cancellation.",
            trial_count=2,
            max_parallel_pairs=1,
        )
    )
    contracts: dict[str, PairedEvalContract] = {}
    effects: list[tuple[str, str]] = []

    def create_pair(child_request):
        eval_id = paired_eval_id(child_request.request_id)
        effects.append(("create", eval_id))
        contract = _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=child_request.model_budget.model_dump(mode="json"),
        )
        contracts[eval_id] = contract
        _record_pair_commit(store, contract)
        return PairedEvalCreated(
            eval_id=eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    callbacks = {
        "create_pair": create_pair,
        "pair_contract": contracts.__getitem__,
        "release_pair": lambda eval_id: effects.append(("release", eval_id)),
        "pair_report": lambda _eval_id: pytest.fail("cancelled campaign read Pair"),
    }
    assert service.advance_one(created.campaign_id, **callbacks)
    assert service.advance_one(created.campaign_id, **callbacks)
    assert [kind for kind, _ in effects] == ["create"]

    with pytest.raises(RuntimeError, match="no cancellation callback"):
        service.cancel(created.campaign_id, reason="unsafe_parent_only_cancel")

    first = service.cancel(
        created.campaign_id,
        reason="operator_requested",
        cancel_pair=lambda eval_id: effects.append(("cancel", eval_id)),
    )
    replay = EvalCampaignService(SQLiteEventStore(tmp_path / "campaign.db"))
    second = replay.cancel(created.campaign_id, reason="duplicate_request")

    assert first == second
    assert first.status == "cancelled"
    assert not replay.advance_one(created.campaign_id, **callbacks)
    assert replay.finalize(created.campaign_id).status == "cancelled"
    assert effects == [
        ("create", next(iter(contracts))),
        ("cancel", next(iter(contracts))),
    ]
    assert (
        sum(
            event.type == "eval.campaign.cancelled"
            for event in store.read_all(run_id=created.campaign_id)
        )
        == 1
    )


def test_pair_creation_failure_is_persisted_and_releases_the_active_slot(tmp_path):
    database = tmp_path / "campaign.db"
    store = SQLiteEventStore(database)
    service = EvalCampaignService(store)
    created = service.create(
        EvalCampaignRequest(
            request_id="campaign-create-failure-1",
            title="Continue after one Pair cannot be created",
            brief="A terminal child preparation failure must not consume the slot.",
            trial_count=2,
            max_parallel_pairs=1,
        )
    )
    contracts: dict[str, PairedEvalContract] = {}

    def create_pair(child_request):
        if child_request.parent_trial_index == 1:
            raise RuntimeError("fixture preparation exploded")
        eval_id = paired_eval_id(child_request.request_id)
        contract = _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=child_request.model_budget.model_dump(mode="json"),
        )
        contracts[eval_id] = contract
        _record_pair_commit(store, contract)
        return PairedEvalCreated(
            eval_id=eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    callbacks = {
        "create_pair": create_pair,
        "pair_contract": contracts.__getitem__,
        "release_pair": lambda _eval_id: None,
        "pair_report": lambda _eval_id: pytest.fail("Pair is not released yet"),
    }
    assert service.advance_one(created.campaign_id, **callbacks)
    assert service.advance_one(created.campaign_id, **callbacks)

    failed = service.report(created.campaign_id)
    failure_events = [
        event
        for event in store.read_all(run_id=created.campaign_id)
        if event.type == "eval.campaign.trial.creation_failed"
    ]
    assert len(failure_events) == 1
    assert failure_events[0].payload["error_type"] == "RuntimeError"
    assert failure_events[0].payload["error_message"] == (
        "fixture preparation exploded"
    )
    assert failed.trials[0].status == "creation_failed"
    assert failed.completed_trial_count == 1
    assert failed.invalid_trial_count == 1

    # A fresh service reconstructs the released slot solely from persisted facts.
    replay = EvalCampaignService(SQLiteEventStore(database))
    assert replay.advance_one(created.campaign_id, **callbacks)
    assert replay.report(created.campaign_id).trials[1].status == "started"
    assert replay.advance_one(created.campaign_id, **callbacks)
    assert replay.report(created.campaign_id).trials[1].status == "linked"


@pytest.mark.parametrize(
    "transient_error",
    (
        TimeoutError("paired eval creation is already in progress"),
        sqlite3.OperationalError("database is locked"),
    ),
    ids=("claim-conflict", "sqlite-lock"),
)
def test_retryable_pair_creation_failure_is_persisted_and_retried_after_restart(
    tmp_path,
    transient_error,
):
    database = tmp_path / "campaign.db"
    store = SQLiteEventStore(database)
    service = EvalCampaignService(store)
    created = service.create(
        EvalCampaignRequest(
            request_id="campaign-retryable-create-1",
            title="Recover transient Pair creation",
            brief="A temporary child creation conflict must survive restart.",
            trial_count=1,
        )
    )
    contracts: dict[str, PairedEvalContract] = {}
    attempts = 0

    def create_pair(child_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise transient_error
        eval_id = paired_eval_id(child_request.request_id)
        contract = _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=child_request.model_budget.model_dump(mode="json"),
        )
        contracts[eval_id] = contract
        _record_pair_commit(store, contract)
        return PairedEvalCreated(
            eval_id=eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    callbacks = {
        "create_pair": create_pair,
        "pair_contract": contracts.__getitem__,
        "release_pair": lambda _eval_id: None,
        "pair_report": lambda _eval_id: pytest.fail("Pair is not released yet"),
    }
    assert service.advance_one(created.campaign_id, **callbacks)
    assert service.advance_one(created.campaign_id, **callbacks)

    retry_events = [
        event
        for event in store.read_all(run_id=created.campaign_id)
        if event.type == "eval.campaign.trial.creation_retryable_failed"
    ]
    assert len(retry_events) == 1
    assert retry_events[0].payload["attempt_number"] == 1
    assert retry_events[0].payload["max_attempts"] == 3
    assert retry_events[0].payload["error_type"] == type(transient_error).__name__
    assert not any(
        event.type == "eval.campaign.trial.creation_failed"
        for event in store.read_all(run_id=created.campaign_id)
    )
    assert service.report(created.campaign_id).trials[0].status == "started"
    assert service.report(created.campaign_id).completed_trial_count == 0

    replay = EvalCampaignService(SQLiteEventStore(database))
    assert replay.advance_one(created.campaign_id, **callbacks)
    assert attempts == 2
    assert replay.report(created.campaign_id).trials[0].status == "linked"


def test_retryable_pair_creation_failure_becomes_terminal_after_bounded_attempts(
    tmp_path,
):
    store = SQLiteEventStore(tmp_path / "campaign.db")
    service = EvalCampaignService(store)
    created = service.create(
        EvalCampaignRequest(
            request_id="campaign-retry-exhausted-1",
            title="Bound transient Pair creation retries",
            brief="A permanently busy child creator must not loop forever.",
            trial_count=1,
        )
    )
    attempts = 0

    def create_pair(_child_request):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("paired eval creation is already in progress")

    callbacks = {
        "create_pair": create_pair,
        "pair_contract": lambda _eval_id: pytest.fail("Pair was not created"),
        "release_pair": lambda _eval_id: pytest.fail("Pair was not linked"),
        "pair_report": lambda _eval_id: pytest.fail("Pair was not released"),
    }
    assert service.advance_one(created.campaign_id, **callbacks)
    for _ in range(3):
        assert service.advance_one(created.campaign_id, **callbacks)

    report = service.report(created.campaign_id)
    terminal = [
        event
        for event in store.read_all(run_id=created.campaign_id)
        if event.type == "eval.campaign.trial.creation_failed"
    ]
    assert attempts == 3
    assert len(terminal) == 1
    assert terminal[0].payload["attempt_number"] == 3
    assert terminal[0].payload["max_attempts"] == 3
    assert terminal[0].payload["retryable"] is True
    assert terminal[0].payload["invalid_reasons"] == ["pair_creation_retry_exhausted"]
    assert report.trials[0].status == "creation_failed"
    assert report.completed_trial_count == 1
    assert report.invalid_trial_count == 1

    assert not service.advance_one(created.campaign_id, **callbacks)
    assert attempts == 3


def test_completed_pair_is_observed_and_campaign_finalizes_once(tmp_path):
    store = SQLiteEventStore(tmp_path / "campaign.db")
    service = EvalCampaignService(store)
    created = service.create(
        EvalCampaignRequest(
            request_id="campaign-finalize-1",
            title="Repeat one repair",
            brief="Persist one deterministic sample and report.",
            trial_count=1,
        )
    )
    contracts: dict[str, PairedEvalContract] = {}
    reports: dict[str, PairedEvalReport] = {}

    def create_pair(child_request):
        eval_id = paired_eval_id(child_request.request_id)
        contract = _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=child_request.model_budget.model_dump(mode="json"),
        )
        contracts[eval_id] = contract
        _record_pair_commit(store, contract)
        return PairedEvalCreated(
            eval_id=eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    callbacks = {
        "create_pair": create_pair,
        "pair_contract": contracts.__getitem__,
        "release_pair": lambda _eval_id: None,
        "pair_report": reports.__getitem__,
    }
    assert service.advance_one(created.campaign_id, **callbacks)
    assert not contracts
    assert service.advance_one(created.campaign_id, **callbacks)
    eval_id = next(iter(contracts))
    reports[eval_id] = PairedEvalReport(
        eval_id=eval_id,
        status="running",
        contract=contracts[eval_id],
        single=PairedEvalArmReport(
            execution_mode="single",
            run_id=contracts[eval_id].single.run_id,
            status="queued",
        ),
        team=PairedEvalArmReport(
            execution_mode="team",
            run_id=contracts[eval_id].team.run_id,
            status="queued",
        ),
    )
    assert service.advance_one(created.campaign_id, **callbacks)

    completed_pair = _completed_pair_report(contracts[eval_id])
    reports[eval_id] = completed_pair
    store.append(
        Event(
            id=f"pair-completed-{eval_id}",
            run_id=eval_id,
            task_id=eval_id,
            type="eval.pair.completed",
            source="runtime.eval",
            payload={"report": completed_pair.model_dump(mode="json")},
        )
    )

    assert service.advance_one(created.campaign_id, **callbacks)
    first = service.finalize(created.campaign_id)
    replay = service.finalize(created.campaign_id)

    assert replay == first
    assert first.status == "completed"
    assert first.completed_trial_count == 1
    assert first.invalid_trial_count == 0
    assert first.aggregate is not None
    assert first.aggregate.sample_count == 1
    assert (
        first.recommendation.outcome is RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE
    )
    assert (
        sum(event.type == "eval.campaign.trial.observed" for event in store.read_all())
        == 1
    )
    assert (
        sum(event.type == "eval.campaign.completed" for event in store.read_all()) == 1
    )
    assert service.cancel(created.campaign_id) == first
    assert not any(
        event.type == "eval.campaign.cancelled" for event in store.read_all()
    )


def test_finalize_rejects_aggregator_version_drift_before_aggregation(tmp_path):
    store = SQLiteEventStore(tmp_path / "campaign.db")
    service = EvalCampaignService(store)
    created = service.create(
        EvalCampaignRequest(
            request_id="campaign-aggregator-drift-1",
            title="Freeze the statistical implementation",
            brief="Do not mix a v1 contract with a v2 aggregator.",
            trial_count=1,
        )
    )
    contracts: dict[str, PairedEvalContract] = {}
    reports: dict[str, PairedEvalReport] = {}

    def create_pair(child_request):
        eval_id = paired_eval_id(child_request.request_id)
        contract = _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=child_request.model_budget.model_dump(mode="json"),
        )
        contracts[eval_id] = contract
        _record_pair_commit(store, contract)
        return PairedEvalCreated(
            eval_id=eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    callbacks = {
        "create_pair": create_pair,
        "pair_contract": contracts.__getitem__,
        "release_pair": lambda _eval_id: None,
        "pair_report": reports.__getitem__,
    }
    assert service.advance_one(created.campaign_id, **callbacks)
    assert service.advance_one(created.campaign_id, **callbacks)
    eval_id = next(iter(contracts))
    assert service.advance_one(created.campaign_id, **callbacks)
    completed_pair = _completed_pair_report(contracts[eval_id])
    reports[eval_id] = completed_pair
    store.append(
        Event(
            id=f"pair-completed-{eval_id}",
            run_id=eval_id,
            task_id=eval_id,
            type="eval.pair.completed",
            source="runtime.eval",
            payload={"report": completed_pair.model_dump(mode="json")},
        )
    )
    assert service.advance_one(created.campaign_id, **callbacks)

    class DriftedAggregator:
        VERSION = "paired-bootstrap-v2"

        def aggregate(self, **_kwargs):
            pytest.fail("version drift must be rejected before aggregation")

    service.aggregator = DriftedAggregator()
    report = service.finalize(created.campaign_id)

    assert report.status == "completed"
    assert report.evidence_valid is False
    assert report.aggregate is None
    assert report.recommendation is not None
    assert (
        report.recommendation.outcome
        is RecommendationOutcome.INSUFFICIENT_LIVE_EVIDENCE
    )
    assert report.invalid_reasons == (
        "aggregator_version_mismatch:paired-bootstrap-v1!=paired-bootstrap-v2",
    )
    assert "missing_aggregate" in report.recommendation.failed_thresholds
    assert (
        EvalCampaignService(SQLiteEventStore(tmp_path / "campaign.db")).finalize(
            created.campaign_id
        )
        == report
    )


def test_campaign_records_child_without_pair_commit_as_creation_failed(tmp_path):
    store = SQLiteEventStore(tmp_path / "campaign.db")
    service = EvalCampaignService(store)
    created = service.create(
        EvalCampaignRequest(
            request_id="campaign-uncommitted-child-1",
            title="Reject incomplete child",
            brief="A created contract is not yet a committed Pair.",
            trial_count=1,
        )
    )
    contracts: dict[str, PairedEvalContract] = {}

    def create_pair(child_request):
        eval_id = paired_eval_id(child_request.request_id)
        contract = _pair_contract(
            eval_id=eval_id,
            root=tmp_path,
            model_budget=child_request.model_budget.model_dump(mode="json"),
        )
        contracts[eval_id] = contract
        return PairedEvalCreated(
            eval_id=eval_id,
            single_run_id=contract.single.run_id,
            team_run_id=contract.team.run_id,
        )

    assert service.advance_one(
        created.campaign_id,
        create_pair=create_pair,
        pair_contract=contracts.__getitem__,
        release_pair=lambda _eval_id: None,
        pair_report=lambda _eval_id: pytest.fail("report should not be read"),
    )
    assert service.advance_one(
        created.campaign_id,
        create_pair=create_pair,
        pair_contract=contracts.__getitem__,
        release_pair=lambda _eval_id: None,
        pair_report=lambda _eval_id: pytest.fail("report should not be read"),
    )
    assert not any(
        event.type == "eval.campaign.trial.linked" for event in store.read_all()
    )
    assert service.report(created.campaign_id).trials[0].status == "creation_failed"
