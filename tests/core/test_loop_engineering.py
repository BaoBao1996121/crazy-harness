from crazy_harness.core.agents.completion import (
    CompletionFindingCode,
    CompletionGate,
    NudgeBudget,
    NudgeKind,
    ProgressDetector,
)
from crazy_harness.core.agents.contracts import AssignmentContract
from crazy_harness.core.agents.planning import (
    PlanEvent,
    PlanEventType,
    PlanStep,
    PlanStepStatus,
    reduce_plan,
)


def assignment_contract() -> AssignmentContract:
    return AssignmentContract(
        version=2,
        goal="Produce a tested release assessment",
        exit_criteria=["The unit test result is reported"],
        output_schema={
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
            "additionalProperties": False,
        },
        evidence_requirements=["unit_tests"],
        constraints=["Do not deploy"],
        permissions=["test.run"],
        budgets={"turns": 8, "tool_calls": 4},
        dependencies=[],
    )


def test_completion_gate_rejects_premature_submission() -> None:
    result = CompletionGate().evaluate(
        assignment_contract(),
        output={},
        evidence={},
        pending_operations=["op-test-run"],
    )

    assert result.passed is False
    assert {finding.code for finding in result.findings} == {
        CompletionFindingCode.SCHEMA,
        CompletionFindingCode.EVIDENCE,
        CompletionFindingCode.PENDING_OPERATION,
    }


def test_completed_local_plan_does_not_complete_assignment() -> None:
    plan = reduce_plan(
        [
            PlanEvent(
                type=PlanEventType.CREATED,
                steps=[
                    PlanStep(step_id="inspect", description="Inspect the change"),
                    PlanStep(step_id="obsolete", description="Run an obsolete check"),
                    PlanStep(step_id="optional", description="Collect optional context"),
                ],
            ),
            PlanEvent(type=PlanEventType.STEP_STARTED, step_id="inspect"),
            PlanEvent(
                type=PlanEventType.STEP_COMPLETED,
                step_id="inspect",
                evidence_refs=["event://inspection"],
            ),
            PlanEvent(
                type=PlanEventType.REVISED,
                steps=[
                    PlanStep(step_id="inspect", description="Inspect the change"),
                    PlanStep(step_id="optional", description="Collect optional context"),
                    PlanStep(step_id="test", description="Run unit tests"),
                ],
            ),
            PlanEvent(type=PlanEventType.STEP_CANCELLED, step_id="optional"),
            PlanEvent(type=PlanEventType.STEP_STARTED, step_id="test"),
            PlanEvent(
                type=PlanEventType.STEP_COMPLETED,
                step_id="test",
                evidence_refs=["event://unit-tests"],
            ),
        ]
    )

    steps = {step.step_id: step for step in plan.steps}
    assert plan.version == 2
    assert steps["obsolete"].status is PlanStepStatus.SUPERSEDED
    assert steps["optional"].status is PlanStepStatus.CANCELLED
    assert plan.is_complete is True

    gate_result = CompletionGate().evaluate(
        assignment_contract(),
        output={"summary": "All active plan steps are complete"},
        evidence={},
    )
    assert gate_result.passed is False
    assert {finding.code for finding in gate_result.findings} == {CompletionFindingCode.EVIDENCE}


def test_repeated_action_without_evidence_delta_is_no_progress() -> None:
    detector = ProgressDetector()
    detector.observe(
        {
            "type": "call_tool",
            "tool_name": "test.run",
            "tool_args": {"suite": "unit", "retries": 0},
        },
        evidence_refs=[],
    )

    repeated = detector.observe(
        {
            "tool_args": {"retries": 0, "suite": "unit"},
            "tool_name": "test.run",
            "type": "call_tool",
        },
        evidence_refs=[],
    )

    assert repeated.repeated_action is True
    assert repeated.evidence_delta == ()
    assert repeated.made_progress is False

    with_new_evidence = detector.observe(
        {
            "type": "call_tool",
            "tool_name": "test.run",
            "tool_args": {"suite": "unit", "retries": 0},
        },
        evidence_refs=["artifact://unit-test-report"],
    )
    assert with_new_evidence.repeated_action is True
    assert with_new_evidence.evidence_delta == ("artifact://unit-test-report",)
    assert with_new_evidence.made_progress is True


def test_nudge_budget_is_typed_and_bounded_per_reason() -> None:
    budget = NudgeBudget(schema=0, evidence=1, pending_operation=1, no_progress=2)

    assert budget.consume(NudgeKind.NO_PROGRESS) is True
    assert budget.consume(NudgeKind.NO_PROGRESS) is True
    assert budget.consume(NudgeKind.NO_PROGRESS) is False
    assert budget.remaining(NudgeKind.NO_PROGRESS) == 0

    assert budget.remaining(NudgeKind.EVIDENCE) == 1
    assert budget.consume(NudgeKind.EVIDENCE) is True
    assert budget.consume(NudgeKind.EVIDENCE) is False
    assert budget.consume(NudgeKind.SCHEMA) is False
