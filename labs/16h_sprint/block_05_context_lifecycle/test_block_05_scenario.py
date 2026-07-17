from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = Path(__file__).with_name("scenario.py")


def test_context_lifecycle_scenario_writes_auditable_evidence(tmp_path: Path) -> None:
    output = tmp_path / "block_05"

    completed = subprocess.run(
        [sys.executable, str(SCENARIO), "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    evidence_path = output / "evidence.json"
    markdown_path = output / "evidence.md"
    assert evidence_path.is_file()
    assert markdown_path.is_file()

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["scenario"] == "block_05_context_lifecycle"
    assert evidence["checks"] and all(evidence["checks"].values())

    states = evidence["microcompact"]["states"]
    assert [state["representation"] for state in states] == [
        "artifact_ref",
        "inline",
        "inline",
        "artifact_ref",
    ]
    assert [state["hydration_turns_remaining"] for state in states] == [0, 1, 0, 0]
    assert evidence["microcompact"]["roundtrip_exact"] is True

    compact = evidence["full_compact"]
    assert compact["dimension_count"] == 9
    assert len(compact["dimensions"]) == 9
    assert compact["raw_events_preserved"] is True
    assert compact["compacted_prefix_count"] > 0
    assert compact["recent_suffix_count"] > 0

    history = evidence["history"]
    assert history["authorized_match_refs"] == [history["shared_ref"]]
    assert history["private_access_denied"] is True

    event_log = output / evidence["events"]["path"]
    offloaded_artifact = output / evidence["microcompact"]["artifact_path"]
    compact_artifact = output / compact["summary_artifact_path"]
    assert len(event_log.read_text(encoding="utf-8").splitlines()) == evidence["events"]["count"]
    assert len(offloaded_artifact.read_text(encoding="utf-8")) == evidence["microcompact"]["original_chars"]
    assert compact_artifact.is_file()
