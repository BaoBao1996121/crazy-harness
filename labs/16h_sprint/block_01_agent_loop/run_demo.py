from __future__ import annotations

import json
from pathlib import Path

from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.runtime import Runtime
from crazy_harness.core.tools import ToolRegistry
from crazy_harness.worlds.cicd.tools import register_cicd_tools
from naive_loop import run_naive_loop


def responses() -> list[str]:
    return [
        json.dumps({"type": "call_tool", "reason": "inspect", "tool_name": "repo.read", "tool_args": {"path": "app.py"}}),
        json.dumps({"type": "stop", "reason": "baseline done"}),
    ]


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    repo = root / "examples" / "hello-crazy-api"
    output = root / "runs" / "learning_block_01"
    output.mkdir(parents=True, exist_ok=True)

    tools = ToolRegistry()
    register_cicd_tools(tools, repo)
    naive = run_naive_loop(FakeModelProvider(responses()), tools)
    (output / "naive_trace.json").write_text(json.dumps(naive, ensure_ascii=False, indent=2), encoding="utf-8")

    runtime = Runtime(mode="mock", repo_path=repo, runs_dir=output / "known_good_runs")
    run_dir = runtime.run()
    print(f"naive_trace={output / 'naive_trace.json'}")
    print(f"known_good_events={run_dir / 'events.jsonl'}")
    print(f"known_good_report={run_dir / 'report.md'}")


if __name__ == "__main__":
    main()
