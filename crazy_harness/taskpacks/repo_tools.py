from __future__ import annotations

import difflib
import os
import shutil
import sys
import time
from pathlib import Path
from uuid import uuid4

from crazy_harness.core.runtime.local import GuardedLocalRuntime
from crazy_harness.core.tools import ToolRegistry, ToolResult, ToolSpec

_MAX_TEXT_CHARS = 200_000
_IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git"}
_ATOMIC_REPLACE_RETRY_SECONDS = (0.02, 0.05)


def build_repo_tools(
    workspace: Path,
    baseline: Path,
    *,
    writable_paths: frozenset[str],
) -> ToolRegistry:
    root = workspace.resolve()
    baseline_root = baseline.resolve()
    runtime = GuardedLocalRuntime(
        root,
        allowed_commands={Path(sys.executable).name},
        max_timeout_seconds=60,
    )
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="repo.list",
            description="List files in the disposable repository.",
            input_schema={"type": "object", "additionalProperties": False},
            use_when="Need to discover repository structure before reading files.",
            side_effect_level="none",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda _: _list_files(root),
    )
    registry.register(
        ToolSpec(
            name="repo.read",
            description="Read one UTF-8 text file inside the disposable repository.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string", "minLength": 1}},
                "required": ["path"],
                "additionalProperties": False,
            },
            use_when="Need exact source, test, or configuration evidence.",
            side_effect_level="none",
            output_offload_policy="offload_if_large",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda args: _read_file(root, str(args.get("path", ""))),
    )
    registry.register(
        ToolSpec(
            name="repo.search",
            description="Search for a literal text fragment in UTF-8 repository files.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string", "minLength": 1}},
                "required": ["query"],
                "additionalProperties": False,
            },
            use_when="Need to find symbols or behavior without reading every file.",
            side_effect_level="none",
            output_offload_policy="offload_if_large",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda args: _search(root, str(args.get("query", ""))),
    )
    registry.register(
        ToolSpec(
            name="repo.write",
            description="Atomically replace an allowlisted implementation file with UTF-8 content.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            use_when="Need to apply a complete, bounded implementation edit.",
            do_not_use_when="The target is a test, policy file, or outside the disposable workspace.",
            side_effect_level="workspace_write",
            approval_required=True,
            is_destructive=True,
            is_concurrency_safe=False,
        ),
        lambda args: _write_file(
            root,
            str(args.get("path", "")),
            str(args.get("content", "")),
            writable_paths,
        ),
    )
    registry.register(
        ToolSpec(
            name="repo.replace",
            description="Atomically replace one exact text fragment in an allowlisted implementation file.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "old": {"type": "string", "minLength": 1},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
                "additionalProperties": False,
            },
            use_when="A small exact replacement is safer than rewriting the full file.",
            side_effect_level="workspace_write",
            approval_required=True,
            is_destructive=True,
            is_concurrency_safe=False,
        ),
        lambda args: _replace_text(
            root,
            str(args.get("path", "")),
            str(args.get("old", "")),
            str(args.get("new", "")),
            writable_paths,
        ),
    )
    registry.register(
        ToolSpec(
            name="test.run",
            description="Run the repository unittest suite in the disposable workspace.",
            input_schema={"type": "object", "additionalProperties": False},
            use_when="Need machine evidence that the implementation satisfies the tests.",
            side_effect_level="local_process",
            output_offload_policy="offload_if_large",
            is_concurrency_safe=False,
        ),
        lambda _: _run_tests(runtime, root),
    )
    registry.register(
        ToolSpec(
            name="repo.diff",
            description="Compare the disposable workspace with its immutable baseline.",
            input_schema={"type": "object", "additionalProperties": False},
            use_when="Need persisted evidence of the actual source change before submission.",
            side_effect_level="none",
            output_offload_policy="offload_if_large",
            is_read_only=True,
            is_concurrency_safe=True,
        ),
        lambda _: _diff_workspace(root, baseline_root),
    )
    registry.register(
        ToolSpec(
            name="shell.run",
            description="Run one allowlisted command profile without a shell string.",
            input_schema={
                "type": "object",
                "properties": {"profile": {"type": "string", "enum": ["python_compile"]}},
                "required": ["profile"],
                "additionalProperties": False,
            },
            use_when="Need a bounded syntax compilation check in addition to tests.",
            side_effect_level="local_process",
            output_offload_policy="offload_if_large",
            is_concurrency_safe=False,
        ),
        lambda args: _run_shell_profile(runtime, str(args.get("profile", ""))),
    )
    return registry


def _list_files(root: Path) -> ToolResult:
    files = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and not (_IGNORED_PARTS & set(path.parts))]
    return ToolResult(name="repo.list", status="ok", output="\n".join(sorted(files)))


def _read_file(root: Path, relative: str) -> ToolResult:
    try:
        target, normalized = _resolve_file(root, relative)
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        return ToolResult(name="repo.read", status="error", error=str(exc))
    if len(text) > _MAX_TEXT_CHARS:
        return ToolResult(name="repo.read", status="error", error=f"file is too large: {normalized}")
    return ToolResult(name="repo.read", status="ok", output=text)


def _search(root: Path, query: str) -> ToolResult:
    if not query:
        return ToolResult(name="repo.search", status="error", error="query must not be empty")
    matches: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or _IGNORED_PARTS & set(path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for number, line in enumerate(lines, start=1):
            if query in line:
                matches.append(f"{path.relative_to(root).as_posix()}:{number}:{line}")
                if len(matches) >= 200:
                    return ToolResult(name="repo.search", status="ok", output="\n".join(matches))
    return ToolResult(name="repo.search", status="ok", output="\n".join(matches) or "no matches")


def _write_file(root: Path, relative: str, content: str, writable_paths: frozenset[str]) -> ToolResult:
    try:
        target, normalized = _resolve_file(root, relative)
        _require_writable(normalized, writable_paths)
        if len(content) > _MAX_TEXT_CHARS:
            raise ValueError("content is too large")
        _atomic_write(target, content)
    except (OSError, UnicodeError, ValueError) as exc:
        return ToolResult(name="repo.write", status="error", error=str(exc))
    return ToolResult(name="repo.write", status="ok", output=f"updated {normalized} ({len(content)} chars)")


def _replace_text(root: Path, relative: str, old: str, new: str, writable_paths: frozenset[str]) -> ToolResult:
    try:
        target, normalized = _resolve_file(root, relative)
        _require_writable(normalized, writable_paths)
        text = target.read_text(encoding="utf-8")
        occurrences = text.count(old)
        if occurrences != 1:
            raise ValueError(f"expected exactly one match in {normalized}, found {occurrences}")
        _atomic_write(target, text.replace(old, new, 1))
    except (OSError, UnicodeError, ValueError) as exc:
        return ToolResult(name="repo.replace", status="error", error=str(exc))
    return ToolResult(name="repo.replace", status="ok", output=f"replaced one fragment in {normalized}")


def _diff_workspace(workspace: Path, baseline: Path) -> ToolResult:
    relative_files = _relative_files(workspace) | _relative_files(baseline)
    chunks: list[str] = []
    for relative in sorted(relative_files):
        before = _read_optional_text(baseline / relative)
        after = _read_optional_text(workspace / relative)
        if before == after:
            continue
        chunks.extend(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative}",
                tofile=f"b/{relative}",
            )
        )
    output = "".join(chunks)
    if not output:
        return ToolResult(name="repo.diff", status="error", error="workspace has no changes")
    if len(output) > _MAX_TEXT_CHARS:
        return ToolResult(name="repo.diff", status="error", error="workspace diff is too large")
    return ToolResult(name="repo.diff", status="ok", output=output)


def _run_shell_profile(runtime: GuardedLocalRuntime, profile: str) -> ToolResult:
    if profile != "python_compile":
        return ToolResult(name="shell.run", status="error", error=f"unknown command profile: {profile}")
    return _run_command("shell.run", runtime, [sys.executable, "-m", "compileall", "-q", "."])


def _run_tests(runtime: GuardedLocalRuntime, root: Path) -> ToolResult:
    # Atomic same-size rewrites can share a timestamp tick on Windows. Remove only
    # bytecode caches inside the disposable root so tests always import current source.
    for candidate in tuple(root.rglob("__pycache__")):
        resolved = candidate.resolve()
        if candidate.is_dir() and resolved.is_relative_to(root):
            shutil.rmtree(resolved)
    return _run_command(
        "test.run",
        runtime,
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
    )


def _run_command(name: str, runtime: GuardedLocalRuntime, argv: list[str]) -> ToolResult:
    try:
        completed = runtime.run(argv, timeout_seconds=60)
    except Exception as exc:
        return ToolResult(name=name, status="error", error=str(exc))
    output = completed.stdout + completed.stderr
    return ToolResult(
        name=name,
        status="ok" if completed.returncode == 0 else "error",
        output=output,
        error=None if completed.returncode == 0 else f"exit code {completed.returncode}",
    )


def _resolve_file(root: Path, relative: str) -> tuple[Path, str]:
    if not relative or Path(relative).is_absolute():
        raise ValueError("path must be a non-empty repository-relative path")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path is outside repository")
    if not target.is_file():
        raise ValueError(f"file not found: {relative}")
    return target, target.relative_to(root).as_posix()


def _require_writable(relative: str, writable_paths: frozenset[str]) -> None:
    if relative not in writable_paths:
        raise ValueError(f"path is not writable for this task pack: {relative}")


def _atomic_write(target: Path, content: str) -> None:
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content.encode("utf-8"))
        # Windows 的索引器/防病毒软件可能短暂占用目标文件。这里的 3 次是初始工程值；
        # 只重试原子 rename 的 PermissionError，不重做模型调用或其他外部副作用。
        for attempt in range(len(_ATOMIC_REPLACE_RETRY_SECONDS) + 1):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                if attempt == len(_ATOMIC_REPLACE_RETRY_SECONDS):
                    raise
                time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS[attempt])
    finally:
        temporary.unlink(missing_ok=True)


def _relative_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and not (_IGNORED_PARTS & set(path.parts))
    }


def _read_optional_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError:
        return "<binary file>\n"
