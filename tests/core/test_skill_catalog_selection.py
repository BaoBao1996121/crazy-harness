from pathlib import Path

import pytest

from crazy_harness.core.skills import (
    FileSystemSkillLoader,
    SkillNotFoundError,
    SkillScope,
    SkillSource,
)


def _skill(root: Path, name: str) -> None:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} method\n---\n\nUse {name}.\n",
        encoding="utf-8",
    )


def test_skill_catalog_can_select_a_task_specific_subset(tmp_path):
    _skill(tmp_path, "alpha")
    _skill(tmp_path, "beta")
    catalog = FileSystemSkillLoader().discover(
        (
            SkillSource(
                source_id="project",
                root=tmp_path,
                scope=SkillScope.PROJECT,
                trusted=True,
            ),
        ),
        agent_id="generalist",
    )

    selected = catalog.select(("beta",))

    assert selected.names() == ("beta",)
    assert selected.activate("beta").body == "Use beta.\n"
    with pytest.raises(SkillNotFoundError):
        catalog.select(("missing",))
