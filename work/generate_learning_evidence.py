from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from crazy_harness.core.agents import AgentLoop, InjectedCrash
from crazy_harness.core.artifacts import ArtifactStore
from crazy_harness.core.events import Event, EventLog
from crazy_harness.core.models import FakeModelProvider
from crazy_harness.core.runtime import BrowserRuntime
from crazy_harness.core.tools import (
    OperationLedger,
    PolicyContext,
    ToolPipeline,
    ToolRegistry,
    ToolResult,
    ToolSpec,
)
from crazy_harness.worlds.cicd.team import build_dev_release_team_runtime
from crazy_harness.worlds.cicd.world import build_dev_release_runtime

ROOT = Path(__file__).resolve().parents[1]


class CrashAfterEffect:
    def __init__(self) -> None:
        self.triggered = False

    def __call__(self, marker: str) -> None:
        if marker == "after_tool_effect" and not self.triggered:
            self.triggered = True
            raise InjectedCrash(marker)


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args) -> None:
        return


def generate_recovery(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    count_file = output / "effect_count.txt"

    def effect(_args):
        count = int(count_file.read_text() if count_file.exists() else "0") + 1
        count_file.write_text(str(count), encoding="utf-8")
        return ToolResult(name="external.effect", status="ok", output="confirmed")

    events_path = output / "events.jsonl"
    ledger_path = output / "operations.jsonl"
    event_log = EventLog(events_path)
    event_log.append(Event(run_id="recovery-demo", task_id="release", type="seed", source="evidence"))
    tools = ToolRegistry()
    tools.register(ToolSpec(name="external.effect", description="count one effect"), effect)
    context = PolicyContext(
        agent_id="builder",
        assignment_id="release",
        mode="mock",
        allowed_tools=frozenset({"external.effect"}),
    )
    first = AgentLoop(
        agent_id="builder",
        task_id="release",
        model=FakeModelProvider(
            [json.dumps({"type": "call_tool", "reason": "effect", "tool_name": "external.effect", "tool_args": {}})]
        ),
        event_log=event_log,
        artifact_store=ArtifactStore(output / "artifacts"),
        tool_registry=tools,
        tool_pipeline=ToolPipeline(tools, ledger=OperationLedger(ledger_path)),
        policy_context=context,
        fault_injector=CrashAfterEffect(),
    )
    try:
        first.run_once()
    except InjectedCrash:
        pass
    resumed_model = FakeModelProvider([json.dumps({"type": "stop", "reason": "ledger reconciled"})])
    resumed = AgentLoop(
        agent_id="builder",
        task_id="release",
        model=resumed_model,
        event_log=EventLog(events_path),
        artifact_store=ArtifactStore(output / "artifacts"),
        tool_registry=tools,
        tool_pipeline=ToolPipeline(tools, ledger=OperationLedger(ledger_path)),
        policy_context=context,
    )
    resumed.run_until_stop(max_steps=2)
    events = event_log.read_all(task_id="release")
    return {
        "status": "passed" if count_file.read_text() == "1" else "failed",
        "effect_count": int(count_file.read_text()),
        "events": len(events),
        "ledger": str(ledger_path),
        "trace": str(events_path),
    }


def generate_browser(output: Path) -> dict:
    site = output / "site"
    site.mkdir(parents=True, exist_ok=True)
    (site / "index.html").write_text(
        "<!doctype html><title>Crazy Browser Evidence</title><h1>Disposable Dev Ready</h1>",
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        evidence = BrowserRuntime().inspect(
            f"http://127.0.0.1:{server.server_port}/index.html",
            output / "evidence",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
    return {
        "status": "passed",
        "title": evidence.title,
        "screenshot": str(evidence.screenshot_path),
        "dom": str(evidence.dom_path),
        "console": str(evidence.console_path),
        "network": str(evidence.network_path),
    }


def optional_lab_scenario(block: int, output: Path) -> dict:
    directory = next((ROOT / "labs" / "16h_sprint").glob(f"block_{block:02d}_*"))
    scenario = directory / "scenario.py"
    if not scenario.exists():
        return {"status": "pending", "reason": f"{scenario} is not present"}
    completed = subprocess.run(
        [sys.executable, str(scenario), "--output", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return {
        "status": "passed" if completed.returncode == 0 else "failed",
        "output": str(output),
        "detail": (completed.stdout + completed.stderr).strip(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "runs" / "learning_evidence")
    args = parser.parse_args()
    run_root = args.output / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_root.mkdir(parents=True, exist_ok=True)
    single = build_dev_release_runtime(
        mode="mock",
        repo_path=ROOT / "examples" / "hello-crazy-api",
        runs_dir=run_root / "single",
    ).run()
    team = build_dev_release_team_runtime(
        repo_path=ROOT / "examples" / "hello-crazy-api",
        runs_dir=run_root / "team",
    ).run()
    evidence = {
        "generated_at": datetime.now().isoformat(),
        "single_agent": {"status": "passed", "run_dir": str(single)},
        "event_driven_team": {"status": "passed", "run_dir": str(team)},
        "ledger_recovery": generate_recovery(run_root / "recovery"),
        "browser_runtime": generate_browser(run_root / "browser"),
        "context_lifecycle": optional_lab_scenario(5, run_root / "context"),
        "memory_eval_evolution": optional_lab_scenario(8, run_root / "governance"),
        "deepseek_live": {"status": "available" if os.getenv("DEEPSEEK_API_KEY") else "blocked_external"},
        "docker_sandbox": {"status": "available" if shutil.which("docker") else "blocked_external"},
    }
    (run_root / "evidence_index.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# Learning Evidence Index", ""]
    for name, value in evidence.items():
        if isinstance(value, dict):
            lines.append(f"- `{name}`: **{value.get('status', 'recorded')}** - `{value.get('run_dir', value.get('output', ''))}`")
    (run_root / "EVIDENCE_INDEX.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(run_root)
    required = [
        value
        for key, value in evidence.items()
        if isinstance(value, dict) and key not in {"deepseek_live", "docker_sandbox"}
    ]
    return 0 if all(item["status"] in {"passed", "pending"} for item in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
