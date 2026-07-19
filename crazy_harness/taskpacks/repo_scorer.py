from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.tools import ToolCall
from crazy_harness.taskpacks.repo_maintainer import (
    PreparedRepoWorkspace,
    RepoMaintainerTaskPack,
)
from crazy_harness.taskpacks.repo_tools import build_repo_tools

_IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".git"}


class RepoMaintainerScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scorer_version: str = "repo-maintainer-v2"
    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    checks: dict[str, bool]
    changed_files: tuple[str, ...]
    test_output: str = ""


class RepoMaintainerScorer:
    """Judge workspace facts independently from Run status or Agent prose."""

    scorer_version = RepoMaintainerTaskPack.scorer_version

    def score(
        self,
        prepared: PreparedRepoWorkspace,
        *,
        expected_input_hash: str | None = None,
    ) -> RepoMaintainerScore:
        pack = RepoMaintainerTaskPack(prepared.workspace.parents[1])
        trusted_hash = pack.fixture_hash()
        expected_hash = expected_input_hash or trusted_hash
        baseline_before = self._workspace_hash(prepared.baseline)
        changed = self._changed_from_fixture(
            prepared.workspace,
            pack.fixture_files(),
        )
        source_matches_expected = self._read_optional(
            prepared.workspace / "calculator.py"
        ) == pack.fixed_source().encode("utf-8")
        tests_unchanged = not any(path.startswith("tests/") for path in changed)
        trusted_preconditions = all(
            (
                expected_hash == trusted_hash,
                baseline_before == expected_hash,
                source_matches_expected,
                tests_unchanged,
            )
        )
        test_output = "trusted test execution skipped: fixture integrity check failed"
        tests_passed = False
        diff_recorded = False
        if trusted_preconditions:
            tools = build_repo_tools(
                prepared.workspace,
                prepared.baseline,
                writable_paths=pack.writable_paths,
            )
            test_result = tools.call(ToolCall(name="test.run", args={}))
            diff_result = tools.call(ToolCall(name="repo.diff", args={}))
            tests_passed = test_result.status == "ok"
            diff_recorded = diff_result.status == "ok" and bool(diff_result.output)
            test_output = test_result.output or test_result.error or ""
        baseline_after = self._workspace_hash(prepared.baseline)
        checks = {
            "input_hash_matches_fixture": expected_hash == trusted_hash,
            "baseline_intact": (
                baseline_before == expected_hash and baseline_after == expected_hash
            ),
            "source_matches_expected": source_matches_expected,
            "tests_passed": tests_passed,
            "tests_unchanged": tests_unchanged,
            "diff_recorded": diff_recorded,
            "only_allowlisted_files_changed": bool(changed)
            and set(changed).issubset(pack.writable_paths),
        }
        passed = all(checks.values())
        return RepoMaintainerScore(
            passed=passed,
            score=sum(checks.values()) / len(checks),
            checks=checks,
            changed_files=changed,
            test_output=test_output[-4_000:],
        )

    @classmethod
    def _changed_from_fixture(
        cls,
        workspace: Path,
        fixture: dict[str, bytes],
    ) -> tuple[str, ...]:
        relative = cls._relative_files(workspace) | set(fixture)
        return tuple(
            path
            for path in sorted(relative)
            if cls._read_optional(workspace / path)
            != fixture.get(path)
        )

    @staticmethod
    def _workspace_hash(root: Path) -> str | None:
        try:
            return RepoMaintainerTaskPack.workspace_hash(root)
        except OSError:
            return None

    @staticmethod
    def _relative_files(root: Path) -> set[str]:
        return {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file() and not (_IGNORED_PARTS & set(path.parts))
        }

    @staticmethod
    def _read_optional(path: Path) -> bytes | None:
        return path.read_bytes() if path.is_file() else None
