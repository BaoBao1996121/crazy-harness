from __future__ import annotations

from crazy_harness.core.a2a.messages import AgentCard


def release_team_cards() -> list[AgentCard]:
    return [
        AgentCard(
            agent_id="coordinator",
            role="Coordinate the dev release task, route A2A messages, and decide when the run is done.",
            capabilities=["task_planning", "a2a_routing", "run_report"],
            input_events=["world.event.code_changed", "a2a.message", "artifact.created"],
            output_artifacts=["RunReport", "ReleasePlan"],
        ),
        AgentCard(
            agent_id="scout",
            role="Inspect repository context and produce a risk report.",
            capabilities=["git_diff_analysis", "repo_reading", "risk_reporting"],
            input_events=["a2a.message"],
            output_artifacts=["RiskReport"],
        ),
        AgentCard(
            agent_id="builder",
            role="Run safe local checks and prepare a release plan.",
            capabilities=["test_run", "build_plan", "release_plan"],
            input_events=["a2a.message", "tool.result"],
            output_artifacts=["ToolExecutionResult", "ReleasePlan"],
        ),
        AgentCard(
            agent_id="reviewer",
            role="Review evidence and decide approve, reject, or needs_human.",
            capabilities=["rubric_review", "evidence_check", "approval_gate"],
            input_events=["a2a.message", "artifact.created"],
            output_artifacts=["ReviewDecision"],
        ),
    ]
