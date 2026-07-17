from crazy_harness.core.capabilities import CAPABILITY_SEARCH_TOOL_NAME
from crazy_harness.core.skills import SKILL_ACTIVATE_TOOL_NAME, SkillActivation, SkillScope
from crazy_harness.core.tools import ToolCall
from crazy_harness.taskpacks import repo_tools
from crazy_harness.taskpacks.repo_maintainer import RepoMaintainerTaskPack


def test_repo_maintainer_pack_materializes_failing_disposable_workspace(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path)
    prepared = pack.prepare("run-1")
    tools = pack.build_tools(prepared)

    result = tools.call(ToolCall(name="test.run"))

    assert prepared.workspace.is_dir()
    assert result.status == "error"
    assert "FAILED" in result.output or "failures=" in result.output


def test_repo_maintainer_pack_protects_tests_from_model_writes(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path)
    prepared = pack.prepare("run-2")
    tools = pack.build_tools(prepared)

    result = tools.call(
        ToolCall(
            name="repo.write",
            args={"path": "tests/test_calculator.py", "content": "# disabled\n"},
        )
    )

    assert result.status == "error"
    assert "not writable" in (result.error or "")


def test_repo_diff_is_evidence_only_after_a_real_change(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path)
    prepared = pack.prepare("run-3")
    tools = pack.build_tools(prepared)

    before = tools.call(ToolCall(name="repo.diff"))
    write = tools.call(
        ToolCall(
            name="repo.write",
            args={"path": "calculator.py", "content": "def clamp(value, lower, upper):\n    return value\n"},
        )
    )
    after = tools.call(ToolCall(name="repo.diff"))

    assert before.status == "error"
    assert write.status == "ok"
    assert after.status == "ok" and "calculator.py" in after.output


def test_repo_write_retries_a_transient_windows_atomic_replace_conflict(tmp_path, monkeypatch):
    pack = RepoMaintainerTaskPack(tmp_path)
    prepared = pack.prepare("run-4")
    tools = pack.build_tools(prepared)
    real_replace = repo_tools.os.replace
    attempts = 0

    def flaky_replace(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(13, "transient sharing violation", str(target))
        real_replace(source, target)

    monkeypatch.setattr(repo_tools.os, "replace", flaky_replace)
    result = tools.call(
        ToolCall(
            name="repo.write",
            args={"path": "calculator.py", "content": "def clamp(*args):\n    return 7\n"},
        )
    )

    assert result.status == "ok"
    assert attempts == 2
    assert (prepared.workspace / "calculator.py").read_text(encoding="utf-8").endswith("return 7\n")

def test_repo_maintainer_pack_installs_search_only_for_a_large_catalog(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path, capability_inline_limit=2)
    prepared = pack.prepare("run-search")
    tools = pack.build_tools(prepared)

    assert tools.has(CAPABILITY_SEARCH_TOOL_NAME)

def test_repo_maintainer_pack_exposes_real_project_skill(tmp_path):
    pack = RepoMaintainerTaskPack(tmp_path)
    skills = pack.build_skills()

    assert skills.names() == ("repo-maintainer",)
    entry = skills.entries()[0]
    assert entry.scope is SkillScope.PROJECT
    assert "body" not in entry.model_dump()

    prepared = pack.prepare("run-skill")
    tools = pack.build_tools(prepared, skills=skills)
    result = tools.call(
        ToolCall(name=SKILL_ACTIVATE_TOOL_NAME, args={"name": "repo-maintainer"})
    )
    activation = SkillActivation.model_validate_json(result.output)

    assert result.status == "ok"
    assert activation.name == "repo-maintainer"
    assert "CompletionGate" in activation.body
    assert tools.spec(SKILL_ACTIVATE_TOOL_NAME).is_read_only is True
