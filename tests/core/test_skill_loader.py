from __future__ import annotations

from pathlib import Path

import pytest

from crazy_harness.core.skills import (
    FileSystemSkillLoader,
    SkillChangedError,
    SkillScope,
    SkillSource,
)


def _write_skill(
    root: Path,
    directory: str,
    *,
    name: str | None = None,
    description: str = "Review repository evidence when a maintenance task needs diagnosis.",
    body: str = "Inspect the repository, run tests, and report evidence.",
    allowed_tools: str | None = None,
) -> Path:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True, exist_ok=True)
    tool_line = f"allowed-tools: {allowed_tools}\n" if allowed_tools else ""
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name or directory}\ndescription: {description}\n{tool_line}---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def test_loader_resolves_agent_project_global_precedence_without_disclosing_body(tmp_path):
    global_root = tmp_path / "global"
    project_root = tmp_path / "project"
    agent_root = tmp_path / "agent"
    _write_skill(global_root, "repo-review", description="global description", body="GLOBAL BODY")
    _write_skill(project_root, "repo-review", description="project description", body="PROJECT BODY")
    _write_skill(
        agent_root,
        "repo-review",
        description="agent description",
        body="AGENT BODY",
        allowed_tools="repo.read repo.test",
    )

    catalog = FileSystemSkillLoader().discover(
        [
            SkillSource(source_id="global", root=global_root, scope=SkillScope.GLOBAL, trusted=True),
            SkillSource(source_id="project", root=project_root, scope=SkillScope.PROJECT, trusted=True),
            SkillSource(
                source_id="generalist-local",
                root=agent_root,
                scope=SkillScope.AGENT,
                trusted=True,
                agent_id="generalist",
            ),
        ],
        agent_id="generalist",
    )

    entry = catalog.entries()[0]
    assert entry.name == "repo-review"
    assert entry.description == "agent description"
    assert entry.scope is SkillScope.AGENT
    assert "body" not in entry.model_dump()
    assert [item.code for item in catalog.diagnostics].count("skill_shadowed") == 2

    activation = catalog.activate("repo-review")
    assert activation.body.strip() == "AGENT BODY"
    assert activation.allowed_tools_hint == ("repo.read", "repo.test")


def test_untrusted_source_is_rejected_before_catalog_disclosure(tmp_path):
    root = tmp_path / "untrusted"
    _write_skill(root, "repo-review", body="UNTRUSTED INSTRUCTIONS")

    catalog = FileSystemSkillLoader().discover(
        [SkillSource(source_id="checkout", root=root, scope=SkillScope.PROJECT, trusted=False)],
        agent_id="generalist",
    )

    assert catalog.entries() == ()
    assert [item.code for item in catalog.diagnostics] == ["source_untrusted"]


def test_invalid_directory_name_match_is_skipped_with_a_diagnostic(tmp_path):
    root = tmp_path / "project"
    _write_skill(root, "repo-review", name="different-name")

    catalog = FileSystemSkillLoader().discover(
        [SkillSource(source_id="project", root=root, scope=SkillScope.PROJECT, trusted=True)],
        agent_id="generalist",
    )

    assert catalog.entries() == ()
    assert any(item.code == "skill_invalid" for item in catalog.diagnostics)


def test_activation_rejects_a_skill_changed_after_discovery(tmp_path):
    root = tmp_path / "project"
    skill_file = _write_skill(root, "repo-review", body="ORIGINAL BODY")
    catalog = FileSystemSkillLoader().discover(
        [SkillSource(source_id="project", root=root, scope=SkillScope.PROJECT, trusted=True)],
        agent_id="generalist",
    )
    skill_file.write_text(
        "---\nname: repo-review\ndescription: changed after discovery\n---\n\nREPLACED BODY\n",
        encoding="utf-8",
    )

    with pytest.raises(SkillChangedError, match="refresh"):
        catalog.activate("repo-review")


def test_activation_lists_resources_without_loading_their_contents(tmp_path):
    root = tmp_path / "project"
    _write_skill(root, "repo-review", body="Read references only when needed.")
    reference = root / "repo-review" / "references" / "details.md"
    reference.parent.mkdir(parents=True)
    reference.write_text("SECRET RESOURCE BODY", encoding="utf-8")
    catalog = FileSystemSkillLoader().discover(
        [SkillSource(source_id="project", root=root, scope=SkillScope.PROJECT, trusted=True)],
        agent_id="generalist",
    )

    activation = catalog.activate("repo-review")

    assert activation.resources == ("references/details.md",)
    assert "SECRET RESOURCE BODY" not in activation.model_dump_json()

def test_duplicate_source_ids_are_rejected_before_discovery(tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    _write_skill(first_root, "first-skill")
    _write_skill(second_root, "second-skill")
    loader = FileSystemSkillLoader()

    with pytest.raises(ValueError, match="duplicate Skill source_id"):
        loader.discover(
            [
                SkillSource(
                    source_id="project",
                    root=first_root,
                    scope=SkillScope.PROJECT,
                    trusted=True,
                ),
                SkillSource(
                    source_id="project",
                    root=second_root,
                    scope=SkillScope.PROJECT,
                    trusted=True,
                ),
            ],
            agent_id="generalist",
        )


def test_catalog_limit_stops_all_sources_and_emits_one_diagnostic(tmp_path):
    roots = [tmp_path / name for name in ("alpha", "beta", "gamma")]
    for index, root in enumerate(roots):
        _write_skill(root, f"skill-{index}")

    catalog = FileSystemSkillLoader(max_skills=1).discover(
        [
            SkillSource(
                source_id=root.name,
                root=root,
                scope=SkillScope.PROJECT,
                trusted=True,
            )
            for root in roots
        ],
        agent_id="generalist",
    )

    assert len(catalog.entries()) == 1
    assert [item.code for item in catalog.diagnostics].count("catalog_limit_reached") == 1
