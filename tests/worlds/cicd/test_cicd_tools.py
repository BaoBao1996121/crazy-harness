import subprocess
import sys
from pathlib import Path

from crazy_harness.core.tools import ToolCall, ToolRegistry
from crazy_harness.worlds.cicd.tools import register_cicd_tools


def test_cicd_tools_can_be_imported_before_runtime_package():
    project_root = Path(__file__).resolve().parents[3]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from crazy_harness.worlds.cicd.tools import register_cicd_tools",
        ],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_cicd_build_plan_tool():
    registry = ToolRegistry()
    register_cicd_tools(registry, Path("examples/hello-crazy-api"))

    result = registry.call(ToolCall(name="build.mock_plan"))

    assert result.status == "ok"
    assert "docker build" in result.output


def test_repo_read_stays_inside_target_repo():
    registry = ToolRegistry()
    register_cicd_tools(registry, Path("examples/hello-crazy-api"))

    ok = registry.call(ToolCall(name="repo.read", args={"path": "app.py"}))
    denied = registry.call(ToolCall(name="repo.read", args={"path": "../../pyproject.toml"}))

    assert ok.status == "ok"
    assert "FastAPI" in ok.output
    assert denied.status == "error"
    assert "outside repository" in (denied.error or "")
