import os

import pytest

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest


@pytest.mark.llm
def test_deepseek_repairs_the_resident_repo_maintainer_golden_task(tmp_path):
    if os.getenv("CRAZY_RUN_LLM_TESTS") != "1" or not os.getenv("DEEPSEEK_API_KEY"):
        pytest.skip("set CRAZY_RUN_LLM_TESTS=1 and DEEPSEEK_API_KEY to run the live golden task")

    runtime = ResidentRuntime(tmp_path)
    created = runtime.submit_task(
        TaskRequest(
            title="Repair the disposable calculator",
            brief=(
                "Inspect the repository and tests, identify the implementation defect, make the smallest "
                "allowlisted fix, run the real tests, inspect the diff, and submit structured evidence."
            ),
            execution_mode="single",
            model_mode="deepseek",
            task_pack="repo-maintainer",
        )
    )

    runtime.run_until_idle(max_steps=40)

    events = runtime.store.read_all(run_id=created.run_id)
    successful_tools = {
        event.payload["result"]["name"]
        for event in events
        if event.type == "tool.completed"
    }
    assert runtime.snapshot(created.run_id)["run"]["status"] == "succeeded"
    assert {"test.run", "repo.diff"} <= successful_tools
    assert any(event.type == "completion.gate.passed" for event in events)
