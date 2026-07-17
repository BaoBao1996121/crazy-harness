from __future__ import annotations

from pathlib import Path

from crazy_harness.core.runtime import Runtime


def build_dev_release_runtime(*, mode: str, repo_path: Path, runs_dir: Path) -> Runtime:
    return Runtime(mode=mode, repo_path=repo_path, runs_dir=runs_dir)
