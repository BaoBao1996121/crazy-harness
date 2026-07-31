from __future__ import annotations

import json
from pathlib import Path

from crazy_harness.core.agents import AssignmentContract
from crazy_harness.core.agents.contracts import AssignmentBudget
from crazy_harness.core.checkpoints import WorkspaceSnapshotStore
from crazy_harness.taskpacks.repo_maintainer import (
    PreparedRepoWorkspace,
    RepoMaintainerTaskPack,
)

_INITIAL_SOURCE = """# QUALITY-TODO: remove after behavior is verified
def clamp(value: int, lower: int, upper: int) -> int:
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return min(lower, max(value, upper))
"""

_BEHAVIOR_FIXED_SOURCE = """# QUALITY-TODO: remove after behavior is verified
def clamp(value: int, lower: int, upper: int) -> int:
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return max(lower, min(value, upper))
"""

_FINAL_SOURCE = """def clamp(value: int, lower: int, upper: int) -> int:
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return max(lower, min(value, upper))
"""

_QUALITY_TEMPLATE_FILES = {
    "calculator.py": _INITIAL_SOURCE,
    "tests/__init__.py": "",
    "tests/test_calculator.py": """import unittest

from calculator import clamp


class ClampTests(unittest.TestCase):
    def test_value_inside_range_is_unchanged(self):
        self.assertEqual(clamp(5, 0, 10), 5)

    def test_value_is_clamped_at_both_bounds(self):
        self.assertEqual(clamp(-3, 0, 10), 0)
        self.assertEqual(clamp(15, 0, 10), 10)

    def test_invalid_bounds_are_rejected(self):
        with self.assertRaises(ValueError):
            clamp(5, 10, 0)


if __name__ == "__main__":
    unittest.main()
""",
    "README.md": (
        "Improve this disposable repository one verified step at a time. "
        "Run the tests and record the diff before submitting.\n"
    ),
}


class RepoQualityTaskPack(RepoMaintainerTaskPack):
    """A two-stage child AgentRun pack for the EngineeringLoop golden path."""

    task_pack_id = "repo-quality"
    case_id = "quality-climb-v1"
    scorer_version = "repo-quality-v1"

    @staticmethod
    def fixture_files() -> dict[str, bytes]:
        return {
            relative: content.encode("utf-8")
            for relative, content in _QUALITY_TEMPLATE_FILES.items()
        }

    @staticmethod
    def _write_fixture(root: Path) -> None:
        for relative, content in _QUALITY_TEMPLATE_FILES.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))

    def prepare_from_state(
        self,
        run_id: str,
        state_ref: str,
        snapshots: WorkspaceSnapshotStore,
    ) -> PreparedRepoWorkspace:
        """Materialize both worktree and baseline from one immutable Active state."""

        if state_ref == "loop-pack://initial":
            return self.prepare(run_id)
        if not state_ref.startswith("snapshot://"):
            raise ValueError(f"unsupported repo-quality state ref: {state_ref}")

        object_id = state_ref.removeprefix("snapshot://")
        prepared = PreparedRepoWorkspace(
            workspace=self.data_dir / "workspaces" / run_id,
            baseline=self.data_dir / "baselines" / run_id,
        )
        for target in (prepared.workspace, prepared.baseline):
            if target.exists():
                existing = snapshots.create(target)
                if existing.object_id != object_id:
                    raise RuntimeError(
                        f"existing engineering-loop workspace has the wrong state: {target}"
                    )
            else:
                snapshots.restore(object_id, target)
        return prepared

    @staticmethod
    def assignment_contract() -> AssignmentContract:
        return AssignmentContract(
            goal="improve the current repository candidate by one bounded, verified step",
            exit_criteria=(
                "the intended implementation change is present",
                "the real test command passed",
                "a non-empty baseline diff was recorded",
                "the submitted artifact matches the required schema",
            ),
            output_schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "changed_files": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["summary", "changed_files"],
                "additionalProperties": False,
            },
            evidence_requirements=("test.run", "repo.diff"),
            constraints=(
                "do not modify tests",
                "perform only the persisted engineering-loop candidate step",
                "remain inside the disposable workspace",
            ),
            permissions=(
                "read_repo",
                "write_allowlisted_implementation",
                "run_bounded_checks",
            ),
            budgets=AssignmentBudget(
                turns=12,
                tool_calls=10,
                retries=2,
                wall_time_seconds=180,
            ),
        )

    @staticmethod
    def scripted_responses(
        *,
        run_metadata: dict[str, object] | None = None,
    ) -> list[str]:
        metadata = run_metadata or {}
        iteration = int(metadata.get("engineering_iteration", 1))
        target_source = _BEHAVIOR_FIXED_SOURCE if iteration == 1 else _FINAL_SOURCE
        summary = (
            "Repaired clamp behavior while retaining the tracked quality marker."
            if iteration == 1
            else "Removed the temporary quality marker while preserving verified behavior."
        )
        actions = [
            {
                "type": "call_tool",
                "reason": "load the repository maintenance method",
                "tool_name": "skill.activate",
                "tool_args": {"name": "repo-maintainer"},
            },
            {
                "type": "call_tool",
                "reason": "inspect the active candidate",
                "tool_name": "repo.read",
                "tool_args": {"path": "calculator.py"},
            },
            {
                "type": "call_tool",
                "reason": "apply exactly this iteration's bounded candidate",
                "tool_name": "repo.write",
                "tool_args": {"path": "calculator.py", "content": target_source},
            },
            {
                "type": "call_tool",
                "reason": "prove repository behavior",
                "tool_name": "test.run",
                "tool_args": {},
            },
            {
                "type": "call_tool",
                "reason": "bind the candidate to a baseline diff",
                "tool_name": "repo.diff",
                "tool_args": {},
            },
            {
                "type": "submit_output",
                "reason": "test and diff evidence satisfy the child assignment",
                "artifact": {
                    "summary": summary,
                    "changed_files": ["calculator.py"],
                },
            },
        ]
        return [json.dumps(action) for action in actions]
