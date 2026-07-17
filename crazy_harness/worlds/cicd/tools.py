from __future__ import annotations

import sys
from pathlib import Path

from crazy_harness.core.runtime.local import GuardedLocalRuntime
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec


def _run(name: str, command: list[str], runtime: GuardedLocalRuntime) -> ToolResult:
    try:
        completed = runtime.run(command, timeout_seconds=60)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return ToolResult(name=name, status="error", error=str(exc))

    output = completed.stdout + completed.stderr
    return ToolResult(
        name=name,
        status="ok" if completed.returncode == 0 else "error",
        output=output,
        error=None if completed.returncode == 0 else f"exit code {completed.returncode}",
    )


def _repo_read(repo_path: Path, args: dict) -> ToolResult:
    relative = str(args.get("path", ""))
    root = repo_path.resolve()
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        return ToolResult(name="repo.read", status="error", error="path is outside repository")
    if not target.is_file():
        return ToolResult(name="repo.read", status="error", error=f"file not found: {relative}")
    return ToolResult(name="repo.read", status="ok", output=target.read_text(encoding="utf-8"))


def register_cicd_tools(registry: ToolRegistry, repo_path: Path) -> None:
    runtime = GuardedLocalRuntime(
        repo_path,
        allowed_commands={"git", Path(sys.executable).name},
        max_timeout_seconds=60,
    )
    registry.register(
        ToolSpec(
            name="repo.read",
            description="Read one UTF-8 text file inside the target repository.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            use_when="Need exact source or configuration evidence.",
            side_effect_level="none",
            output_offload_policy="offload_if_large",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda args: _repo_read(repo_path, args),
    )
    registry.register(
        ToolSpec(
            name="git.status",
            description="Run git status for the target repository.",
            use_when="Need current repository state.",
            side_effect_level="none",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda args: _run("git.status", ["git", "status", "--short"], runtime),
    )
    registry.register(
        ToolSpec(
            name="git.diff",
            description="Run git diff for the target repository.",
            use_when="Need changed files and patch context.",
            side_effect_level="none",
            output_offload_policy="offload_if_large",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda args: _run("git.diff", ["git", "diff", "--", "."], runtime),
    )
    registry.register(
        ToolSpec(
            name="test.run",
            description="Run the toy service test suite.",
            use_when="Need release evidence from tests.",
            side_effect_level="local_process",
            output_offload_policy="offload_if_large",
            is_read_only=False,
            is_concurrency_safe=False,
        ),
        lambda args: _run(
            "test.run",
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            runtime,
        ),
    )
    registry.register(
        ToolSpec(
            name="build.mock_plan",
            description="Produce a dry build plan without building or pushing an image.",
            use_when="Need Docker build intent without side effects.",
            side_effect_level="none",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda args: ToolResult(
            name="build.mock_plan",
            status="ok",
            output="Plan: docker build -t crazy-lab/hello-crazy-api:dev .",
        ),
    )
    registry.register(
        ToolSpec(
            name="volcengine.plan",
            description="Produce a Volcengine dry-run deployment plan.",
            use_when="Need cloud deployment intent without touching cloud resources.",
            side_effect_level="none",
            approval_required=False,
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda args: ToolResult(
            name="volcengine.plan",
            status="ok",
            output="Dry-run only: target VKE namespace crazy-dev, image crazy-lab/hello-crazy-api:dev.",
        ),
    )
