from __future__ import annotations

import argparse
import json
from pathlib import Path

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.capabilities import CAPABILITY_SEARCH_TOOL_NAME
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.taskpacks.repo_maintainer import RepoMaintainerTaskPack

_FIXED_SOURCE = """def clamp(value: int, lower: int, upper: int) -> int:
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return max(lower, min(value, upper))
"""


def _action(**payload: object) -> str:
    return json.dumps(payload)


def scripted_responses() -> list[str]:
    """Force the model through search -> disclosure -> direct native call."""

    return [
        _action(
            type="call_tool",
            reason="discover a bounded syntax checker",
            tool_name=CAPABILITY_SEARCH_TOOL_NAME,
            tool_args={"query": "syntax compilation command profile"},
        ),
        _action(
            type="call_tool",
            reason="check the initial source tree compiles",
            tool_name="shell.run",
            tool_args={"profile": "python_compile"},
        ),
        _action(
            type="call_tool",
            reason="discover an exact source reader",
            tool_name=CAPABILITY_SEARCH_TOOL_NAME,
            tool_args={"query": "read UTF-8 exact source"},
        ),
        _action(
            type="call_tool",
            reason="inspect the faulty implementation",
            tool_name="repo.read",
            tool_args={"path": "calculator.py"},
        ),
        _action(
            type="call_tool",
            reason="discover the bounded implementation writer",
            tool_name=CAPABILITY_SEARCH_TOOL_NAME,
            tool_args={"query": "complete bounded implementation edit"},
        ),
        _action(
            type="call_tool",
            reason="apply the bounded repair",
            tool_name="repo.write",
            tool_args={"path": "calculator.py", "content": _FIXED_SOURCE},
        ),
        _action(
            type="call_tool",
            reason="discover the machine test runner",
            tool_name=CAPABILITY_SEARCH_TOOL_NAME,
            tool_args={"query": "unittest machine evidence tests"},
        ),
        _action(
            type="call_tool",
            reason="prove the repaired behavior",
            tool_name="test.run",
            tool_args={},
        ),
        _action(
            type="call_tool",
            reason="discover immutable baseline diff evidence",
            tool_name=CAPABILITY_SEARCH_TOOL_NAME,
            tool_args={"query": "immutable baseline source change evidence"},
        ),
        _action(
            type="call_tool",
            reason="record the real source change",
            tool_name="repo.diff",
            tool_args={},
        ),
        _action(
            type="submit_output",
            reason="tests and diff prove the searched capabilities completed the repair",
            artifact={
                "summary": "Corrected clamp bounds handling after progressive tool discovery.",
                "changed_files": ["calculator.py"],
            },
        ),
    ]


def run_demo(data_dir: Path) -> dict[str, object]:
    model = FakeModelProvider(scripted_responses())
    runtime = ResidentRuntime(data_dir, model_factory=lambda _: model)
    runtime.repo_maintainer_pack = RepoMaintainerTaskPack(
        data_dir,
        capability_inline_limit=2,
        capability_search_limit=2,
    )
    created = runtime.submit_task(
        TaskRequest(
            title="Tool Search 渐进披露实验",
            brief=(
                "Repair the disposable repository, but discover deferred capabilities "
                "through capability.search before direct native calls."
            ),
            execution_mode="single",
            model_mode="scripted",
            task_pack="repo-maintainer",
        )
    )
    steps = runtime.run_until_idle(max_steps=80)
    events = runtime.store.read_all(run_id=created.run_id)
    manifests = [
        event for event in events if event.type == "capability.manifest.compiled"
    ]
    searches = [
        event
        for event in events
        if event.type == "tool.completed"
        and event.payload.get("result", {}).get("name")
        == CAPABILITY_SEARCH_TOOL_NAME
    ]
    snapshot = runtime.snapshot(created.run_id)
    return {
        "run_id": created.run_id,
        "task_id": created.task_id,
        "status": snapshot["run"]["status"],
        "scheduler_steps": steps,
        "model_calls": model.call_count,
        "event_count": len(events),
        "search_calls": len(searches),
        "manifest_count": len(manifests),
        "recalled_tools": sorted(
            {
                name
                for event in manifests
                for name in event.payload["manifest"].get(
                    "recall_sources", {}
                )
            }
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a real ResidentRuntime progressive Tool Search demo."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("runs/capability_search_demo"),
    )
    args = parser.parse_args()
    result = run_demo(args.data_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
