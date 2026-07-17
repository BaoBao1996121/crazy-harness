import pytest
from pydantic import ValidationError

from crazy_harness.core.a2a.messages import A2AMessage, AgentCard
from crazy_harness.core.a2a.coordinator import AgentStatus, Assignment, Coordinator
from crazy_harness.core.a2a.policy import PeerContract, PeerPolicy, PeerRequest
from crazy_harness.core.a2a.review import EvidencePack, ReviewerGate


def test_peer_policy_allows_one_hop_request_within_contract():
    contract = PeerContract(
        assignment_id="a1",
        task_id="t1",
        contract_version=2,
        scope={"repo:read"},
        permissions={"artifact:read"},
        peer_budget=2,
    )
    request = PeerRequest(
        message=A2AMessage(
            task_id="t1",
            context_id="a1",
            sender="builder",
            receiver="scout",
            performative="request",
            instruction="Share the failing-test evidence",
            contract_version=2,
            depth=1,
            intent="evidence",
        ),
        scope={"repo:read"},
        permissions={"artifact:read"},
    )

    decision = PeerPolicy().authorize(request, contract)

    assert decision.allowed is True
    assert decision.remaining_budget == 1


def test_peer_policy_rejects_second_hop_and_ordinary_delegation():
    contract = PeerContract(assignment_id="a1", task_id="t1", peer_budget=2)
    policy = PeerPolicy()
    second_hop = PeerRequest(
        message=A2AMessage(
            task_id="t1",
            context_id="a1",
            sender="scout",
            receiver="reviewer",
            performative="request",
            instruction="Continue the evidence chain",
            depth=2,
            intent="evidence",
        )
    )
    delegation = PeerRequest(
        message=A2AMessage(
            task_id="t1",
            context_id="a1",
            sender="builder",
            receiver="reviewer",
            performative="request",
            instruction="Take a new assignment",
            depth=1,
            intent="delegate",
        )
    )

    depth_decision = policy.authorize(second_hop, contract)
    delegate_decision = policy.authorize(delegation, contract)

    assert (depth_decision.allowed, depth_decision.reason) == (False, "peer_depth_exceeded")
    assert (delegate_decision.allowed, delegate_decision.reason) == (False, "intent_not_allowed")


def test_peer_policy_rejects_scope_and_permission_escalation_without_spending_budget():
    contract = PeerContract(
        assignment_id="a1",
        task_id="t1",
        scope={"repo:read"},
        permissions={"artifact:read"},
        peer_budget=2,
    )
    policy = PeerPolicy()

    def request(*, scope=(), permissions=()):
        return PeerRequest(
            message=A2AMessage(
                task_id="t1",
                context_id="a1",
                sender="builder",
                receiver="scout",
                performative="request",
                instruction="Reconcile evidence",
                depth=1,
                intent="revision",
            ),
            scope=set(scope),
            permissions=set(permissions),
        )

    scope_decision = policy.authorize(request(scope={"repo:write"}), contract)
    permission_decision = policy.authorize(request(permissions={"tool:execute"}), contract)

    assert (scope_decision.allowed, scope_decision.reason) == (False, "scope_escalation")
    assert (permission_decision.allowed, permission_decision.reason) == (False, "permission_escalation")
    assert scope_decision.remaining_budget == permission_decision.remaining_budget == 2


def test_peer_policy_binds_requests_to_current_assignment_contract():
    contract = PeerContract(
        assignment_id="a1",
        task_id="t1",
        contract_version=3,
        peer_budget=1,
    )
    valid_message = A2AMessage(
        task_id="t1",
        context_id="a1",
        sender="builder",
        receiver="scout",
        performative="request",
        instruction="Report progress",
        contract_version=3,
        depth=1,
        intent="progress",
    )
    mismatches = [
        (valid_message.model_copy(update={"task_id": "other"}), "task_mismatch"),
        (valid_message.model_copy(update={"context_id": "other"}), "assignment_mismatch"),
        (valid_message.model_copy(update={"contract_version": 2}), "contract_version_mismatch"),
    ]

    for message, reason in mismatches:
        decision = PeerPolicy().authorize(PeerRequest(message=message), contract)
        assert (decision.allowed, decision.reason) == (False, reason)
        assert decision.remaining_budget == 1


def test_peer_policy_allows_only_budgeted_peer_intents():
    intents = ["evidence", "review", "revision", "block", "progress"]
    contract = PeerContract(assignment_id="a1", task_id="t1", peer_budget=len(intents))
    policy = PeerPolicy()

    def request(intent):
        return PeerRequest(
            message=A2AMessage(
                task_id="t1",
                context_id="a1",
                sender="builder",
                receiver="reviewer",
                performative="request",
                instruction="Bounded peer reconciliation",
                depth=1,
                intent=intent,
            )
        )

    decisions = [policy.authorize(request(intent), contract) for intent in intents]
    exhausted = policy.authorize(request("progress"), contract)

    assert [decision.allowed for decision in decisions] == [True] * len(intents)
    assert (exhausted.allowed, exhausted.reason, exhausted.remaining_budget) == (
        False,
        "peer_budget_exhausted",
        0,
    )


def test_coordinator_selects_an_available_card_from_assignment_requirements():
    cards = [
        AgentCard(
            agent_id="exact-but-offline",
            role="generic worker",
            capabilities=["test_run"],
        ),
        AgentCard(
            agent_id="available-instance",
            role="another generic worker",
            capabilities=["repo_reading", "test_run"],
        ),
        AgentCard(
            agent_id="wrong-capability",
            role="generic worker",
            capabilities=["risk_reporting"],
        ),
    ]
    coordinator = Coordinator(
        cards,
        statuses={"exact-but-offline": AgentStatus.OFFLINE},
    )
    assignment = Assignment(
        assignment_id="a-test",
        task_id="t1",
        goal="Run deterministic tests",
        required_capabilities={"test_run"},
        exit_criteria=["test result is attached"],
    )

    step = coordinator.assign(assignment)

    assert step.agent_id == "available-instance"
    assert step.state == "active"
    assert coordinator.rolling_plan == [step]


def test_coordinator_replans_when_selected_instance_becomes_unavailable():
    cards = [
        AgentCard(
            agent_id="primary-instance",
            role="generic worker",
            capabilities=["test_run"],
        ),
        AgentCard(
            agent_id="standby-instance",
            role="generic worker",
            capabilities=["repo_reading", "test_run"],
        ),
    ]
    coordinator = Coordinator(cards)
    assignment = Assignment(
        assignment_id="a-test",
        task_id="t1",
        goal="Run deterministic tests",
        required_capabilities={"test_run"},
        exit_criteria=["test result is attached"],
    )
    initial = coordinator.assign(assignment)

    changes = coordinator.update_status(
        "primary-instance",
        AgentStatus.OFFLINE,
        reason="heartbeat missed",
    )

    assert initial.agent_id == "primary-instance"
    assert [step.state for step in changes] == ["superseded", "active"]
    assert changes[-1].agent_id == "standby-instance"
    assert coordinator.rolling_plan[-1] == changes[-1]
    assert coordinator.replan_reason == "primary-instance became offline: heartbeat missed"


def test_reviewer_approves_from_evidence_pack_without_worker_transcript():
    worker_transcript = "private scratchpad and tool chatter"
    pack_data = {
        "assignment_id": "a-release",
        "goal": "Validate the release candidate",
        "exit_criteria": ["tests passed", "risk report attached"],
        "candidate_artifact_refs": ["artifact://release-plan"],
        "evidence_by_criterion": {
            "tests passed": ["artifact://pytest-result"],
            "risk report attached": ["artifact://risk-report"],
        },
    }
    pack = EvidencePack(**pack_data)

    decision = ReviewerGate().review(pack)

    assert decision.verdict == "approve"
    assert [item.verdict for item in decision.criteria] == ["approve", "approve"]
    assert "worker_transcript" not in pack.model_dump()
    with pytest.raises(ValidationError):
        EvidencePack(**pack_data, worker_transcript=worker_transcript)


def test_reviewer_requests_revision_for_each_unmet_exit_criterion():
    pack = EvidencePack(
        assignment_id="a-release",
        goal="Validate the release candidate",
        exit_criteria=["tests passed", "risk report attached"],
        candidate_artifact_refs=["artifact://release-plan"],
        evidence_by_criterion={"tests passed": ["artifact://pytest-result"]},
    )

    decision = ReviewerGate().review(pack)

    assert decision.verdict == "revise"
    assert [(item.criterion, item.verdict) for item in decision.criteria] == [
        ("tests passed", "approve"),
        ("risk report attached", "revise"),
    ]
    assert decision.criteria[-1].reason == "missing_evidence"
