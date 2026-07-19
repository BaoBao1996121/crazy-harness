import pytest

from crazy_harness.core.tools import ToolCall
from crazy_harness.taskpacks import RepoMaintainerScorer, RepoMaintainerTaskPack


def test_case_metadata_rejects_a_preexisting_incomplete_workspace(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path)
    workspace = tmp_path / "workspaces" / "run-incomplete"
    workspace.mkdir(parents=True)
    (workspace / "calculator.py").write_text("partial", encoding="utf-8")

    prepared = pack.prepare("run-incomplete")

    with pytest.raises(RuntimeError, match="workspace does not match"):
        pack.case_metadata(prepared)


def test_prepare_never_publishes_a_partially_materialized_fixture(
    tmp_path,
    monkeypatch,
):
    pack = RepoMaintainerTaskPack(tmp_path)

    def interrupt_materialization(root):
        (root / "calculator.py").parent.mkdir(parents=True, exist_ok=True)
        (root / "calculator.py").write_text("partial", encoding="utf-8")
        raise RuntimeError("injected fixture interruption")

    monkeypatch.setattr(pack, "_write_fixture", interrupt_materialization, raising=False)

    with pytest.raises(RuntimeError, match="injected fixture interruption"):
        pack.prepare("run-interrupted")

    assert not (tmp_path / "workspaces" / "run-interrupted").exists()


def test_repo_scorer_uses_workspace_facts_instead_of_agent_claims(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path)
    prepared = pack.prepare("run-scored")
    scorer = RepoMaintainerScorer()

    broken = scorer.score(prepared)
    tools = pack.build_tools(prepared)
    tools.call(
        ToolCall(
            name="repo.write",
            args={"path": "calculator.py", "content": pack.fixed_source()},
        )
    )
    repaired = scorer.score(prepared)

    assert broken.passed is False
    assert broken.checks["tests_passed"] is False
    assert repaired.passed is True
    assert repaired.score == 1.0
    assert repaired.changed_files == ("calculator.py",)


def test_repo_scorer_rejects_test_tampering_even_when_tests_pass(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path)
    prepared = pack.prepare("run-tampered")
    (prepared.workspace / "calculator.py").write_text(
        pack.fixed_source(), encoding="utf-8"
    )
    (prepared.workspace / "tests" / "test_calculator.py").write_text(
        "import unittest\n", encoding="utf-8"
    )

    result = RepoMaintainerScorer().score(prepared)

    assert result.passed is False
    assert result.checks["tests_unchanged"] is False
    assert result.checks["only_allowlisted_files_changed"] is False


def test_repo_scorer_rejects_poisoned_baseline_and_does_not_run_tampered_tests(
    tmp_path,
):
    pack = RepoMaintainerTaskPack(tmp_path)
    prepared = pack.prepare("run-baseline-poisoned")
    expected_input_hash = pack.case_metadata(prepared)["fixture_hash"]
    marker = tmp_path / "tampered-tests-executed"
    malicious_test = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed', encoding='utf-8')\n"
    )
    for root in (prepared.workspace, prepared.baseline):
        (root / "tests" / "test_calculator.py").write_text(
            malicious_test, encoding="utf-8"
        )
    (prepared.workspace / "calculator.py").write_text(
        pack.fixed_source(), encoding="utf-8"
    )

    result = RepoMaintainerScorer().score(
        prepared,
        expected_input_hash=expected_input_hash,
    )

    assert result.passed is False
    assert result.checks["baseline_intact"] is False
    assert result.checks["tests_unchanged"] is False
    assert marker.exists() is False
