from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "runs" / "course_ready"
LEARNER_COMPLETION_MARKER = "LEARNER_COMPLETED.md"
DEFAULT_CHECK_TIMEOUT_SECONDS = 300
REFERENCE_SUITE_TIMEOUT_SECONDS = 600
BUG_CARD_TIMEOUT_SECONDS = 60


@dataclass
class Check:
    name: str
    status: str
    command: str = ""
    detail: str = ""


def run_check(
    name: str,
    args: list[str],
    *,
    expect_failure: bool = False,
    timeout_seconds: int = DEFAULT_CHECK_TIMEOUT_SECONDS,
) -> Check:
    environment = os.environ.copy()
    if "pytest" in args:
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    try:
        completed = subprocess.run(
            args,
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return Check(
            name=name,
            status="failed",
            command=subprocess.list2cmdline(args),
            detail=f"timed out after {timeout_seconds}s",
        )
    passed = completed.returncode != 0 if expect_failure else completed.returncode == 0
    output = (completed.stdout + completed.stderr).strip().splitlines()
    return Check(
        name=name,
        status="passed" if passed else "failed",
        command=subprocess.list2cmdline(args),
        detail="\n".join(output[-8:]),
    )

def bug_card_expects_failure(directory: Path) -> bool:
    """未完成练习应保持红灯；有完成标记的练习应保持绿灯。"""
    return not (directory / LEARNER_COMPLETION_MARKER).exists()


def lab_asset_check() -> Check:
    missing: list[str] = []
    for number in range(1, 9):
        matches = list((ROOT / "labs" / "16h_sprint").glob(f"block_{number:02d}_*"))
        if len(matches) != 1:
            missing.append(f"block {number}: expected one directory")
            continue
        required = {"README.md", "run_demo.py", "PSEUDOCODE_TEMPLATE.md"}
        if number > 1:
            required.add("fault_check.py")
        for filename in required:
            if not (matches[0] / filename).exists():
                missing.append(str(matches[0] / filename))
    return Check(
        name="eight_lab_asset_sets",
        status="passed" if not missing else "failed",
        detail="all eight blocks complete" if not missing else "\n".join(missing),
    )


def main() -> int:
    python = sys.executable
    checks = [
        lab_asset_check(),
        # 2026-07-18 Windows 全套实测约 314 秒；600 秒保留约 2x 余量，
        # 其他检查仍使用 300 秒，避免掩盖局部挂死。
        run_check(
            "reference_suite",
            [python, "-m", "pytest", "-q"],
            timeout_seconds=REFERENCE_SUITE_TIMEOUT_SECONDS,
        ),
        run_check(
            "single_agent_vertical",
            [python, "-m", "crazy_harness.cli", "run", "dev-release", "--mode", "mock", "--runs-dir", "runs/course_ready"],
        ),
        run_check(
            "resident_team_vertical",
            [python, "-m", "crazy_harness.cli", "run", "dev-release", "--team", "--mode", "mock", "--runs-dir", "runs/course_ready"],
        ),
        run_check(
            "real_browser_runtime",
            [python, "-m", "pytest", "-q", "tests/core/test_browser_runtime.py"],
        ),
        run_check(
            "block_01_agent_loop_demo",
            [python, "labs/16h_sprint/block_01_agent_loop/run_demo.py"],
        ),
        run_check(
            "block_05_context_scenario",
            [
                python,
                "labs/16h_sprint/block_05_context_lifecycle/scenario.py",
                "--output",
                "runs/course_ready/context_scenario",
            ],
        ),
        run_check(
            "block_08_governance_scenario",
            [
                python,
                "labs/16h_sprint/block_08_eval_memory_evolution/scenario.py",
                "--output",
                "runs/course_ready/governance_scenario",
            ],
        ),
        run_check(
            "lab_python_compiles",
            [python, "-m", "compileall", "-q", "labs/16h_sprint"],
        ),
        run_check(
            "ruff_static_check",
            [python, "-m", "ruff", "check", "--no-cache", "crazy_harness", "tests", "work", "labs/16h_sprint"],
        ),
    ]

    for block in range(2, 9):
        directory = next((ROOT / "labs" / "16h_sprint").glob(f"block_{block:02d}_*"))
        expect_failure = bug_card_expects_failure(directory)
        expected_state = "starts_red" if expect_failure else "solution_green"
        checks.append(
            run_check(
                f"bug_card_{block}_{expected_state}",
                [python, "-m", "pytest", "-q", str(directory / "fault_check.py")],
                expect_failure=expect_failure,
                timeout_seconds=BUG_CARD_TIMEOUT_SECONDS,
            )
        )

    external = [
        Check(
            name="deepseek_live_api",
            status="available" if os.getenv("DEEPSEEK_API_KEY") else "blocked_external",
            detail="DEEPSEEK_API_KEY is set" if os.getenv("DEEPSEEK_API_KEY") else "DEEPSEEK_API_KEY is not configured",
        ),
        Check(
            name="docker_sandbox_host",
            status="available" if shutil.which("docker") else "blocked_external",
            detail="docker CLI found" if shutil.which("docker") else "docker CLI/engine is unavailable; GuardedLocalRuntime is verified",
        ),
    ]
    required_ok = all(check.status == "passed" for check in checks)
    status = "ready" if required_ok and all(item.status == "available" for item in external) else (
        "ready_with_external_gates" if required_ok else "failed"
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "required_checks": [asdict(check) for check in checks],
        "external_conditions": [asdict(check) for check in external],
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "readiness_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = ["# Course Readiness Report", "", f"- Status: **{status}**", "", "## Required Checks", ""]
    lines.extend(f"- `{item.name}`: **{item.status}**" for item in checks)
    lines.extend(["", "## External Conditions", ""])
    lines.extend(f"- `{item.name}`: **{item.status}** - {item.detail}" for item in external)
    (REPORT_DIR / "readiness_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(REPORT_DIR / "readiness_report.md")
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
