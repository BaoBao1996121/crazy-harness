from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class LocalRuntimeError(RuntimeError):
    pass


class LocalRuntimePolicyError(LocalRuntimeError, PermissionError):
    pass


class LocalRuntimeTimeout(LocalRuntimeError, TimeoutError):
    pass


@dataclass(frozen=True)
class LocalExecutionResult:
    argv: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


class GuardedLocalRuntime:
    """Restricted host subprocess execution. This is explicitly not a sandbox."""

    is_sandbox = False
    _ESSENTIAL_ENV = frozenset(
        {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "HOME"}
    )

    def __init__(
        self,
        workspace: str | Path,
        *,
        allowed_commands: Collection[str],
        allowed_env: Collection[str] = (),
        max_timeout_seconds: float = 30.0,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise LocalRuntimePolicyError(f"workspace is not an existing directory: {self.workspace}")
        if max_timeout_seconds <= 0:
            raise ValueError("max_timeout_seconds must be positive")
        self.allowed_commands = frozenset(_command_name(command) for command in allowed_commands)
        self.allowed_env = frozenset(allowed_env)
        self.max_timeout_seconds = float(max_timeout_seconds)

    def run(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> LocalExecutionResult:
        if isinstance(argv, (str, bytes)) or not argv:
            raise LocalRuntimePolicyError("command must be a non-empty argv sequence; shell strings are forbidden")
        command = tuple(os.fspath(part) for part in argv)
        executable = _command_name(command[0])
        if executable not in self.allowed_commands:
            raise LocalRuntimePolicyError(f"command {executable!r} is not in the command allowlist")

        working_directory = self._resolve_cwd(cwd)
        timeout = self.max_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        if timeout <= 0 or timeout > self.max_timeout_seconds:
            raise LocalRuntimePolicyError(
                f"timeout must be greater than zero and no more than {self.max_timeout_seconds:g} seconds"
            )
        controlled_env = self._build_env(env or {})

        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=working_directory,
                env=controlled_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LocalRuntimeTimeout(f"command timed out after {timeout:g} seconds") from exc

        return LocalExecutionResult(
            argv=command,
            cwd=working_directory,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )

    def _resolve_cwd(self, cwd: str | Path | None) -> Path:
        candidate = self.workspace if cwd is None else Path(cwd)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise LocalRuntimePolicyError(
                f"working directory {resolved} is outside workspace {self.workspace}"
            )
        if not resolved.is_dir():
            raise LocalRuntimePolicyError(f"working directory does not exist: {resolved}")
        return resolved

    def _build_env(self, requested: Mapping[str, str]) -> dict[str, str]:
        forbidden = set(requested) - self.allowed_env
        if forbidden:
            raise LocalRuntimePolicyError(
                f"environment variables are not allowlisted: {sorted(forbidden)!r}"
            )
        inherited = self._ESSENTIAL_ENV | self.allowed_env
        controlled = {key: value for key, value in os.environ.items() if key in inherited}
        controlled.update(requested)
        return controlled


def _command_name(command: str) -> str:
    name = Path(command).name.casefold()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name
