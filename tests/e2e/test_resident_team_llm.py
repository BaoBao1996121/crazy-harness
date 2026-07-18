import os

import pytest

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest


@pytest.mark.llm
def test_deepseek_completes_the_governed_resident_agent_team(tmp_path):
    if os.getenv("CRAZY_RUN_LLM_TESTS") != "1" or not os.getenv(
        "DEEPSEEK_API_KEY"
    ):
        pytest.skip(
            "set CRAZY_RUN_LLM_TESTS=1 and DEEPSEEK_API_KEY to run the live Team task"
        )

    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Complete one governed resident Team task",
            brief=(
                "Collect the required evidence and risk facts, compose the bounded "
                "artifact, use the allowed one-hop peer reconciliation when requested, "
                "and submit only after the independent review evidence is complete."
            ),
            execution_mode="team",
            model_mode="deepseek",
        )
    )

    runtime.run_until_idle(max_steps=240)

    snapshot = runtime.snapshot(created.run_id)
    events = runtime.store.read_all(run_id=created.run_id)
    assert snapshot["run"]["status"] == "succeeded"
    assert snapshot["model_calls"]
    assert all(call["state"] == "completed" for call in snapshot["model_calls"])
    assert snapshot["model_budget"]["active_calls"] == 0
    assert snapshot["model_budget"]["unknown_calls"] == 0
    assert any(event.type == "a2a.peer.responded" for event in events)
    assert any(event.type == "completion.gate.passed" for event in events)
