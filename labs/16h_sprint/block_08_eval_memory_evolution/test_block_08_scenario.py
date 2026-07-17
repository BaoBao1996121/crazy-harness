from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = Path(__file__).with_name("scenario.py")


def test_memory_eval_evolution_scenario_writes_auditable_evidence(tmp_path: Path) -> None:
    output = tmp_path / "block_08"

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
    assert evidence["scenario"] == "block_08_eval_memory_evolution"
    assert evidence["checks"] and all(evidence["checks"].values())

    memory = evidence["memory"]
    assert memory["approved"]["status"] == "approved"
    assert memory["rejected"]["status"] == "rejected"
    assert memory["conflict"]["detected"] is True
    assert memory["conflict"]["candidate_status_after_conflict"] == "candidate"
    assert memory["approved"]["candidate_id"] in memory["recalled_candidate_ids"]

    evaluation = evidence["eval"]
    assert evaluation["baseline_passed"] is True
    assert evaluation["bad_candidate_passed"] is False
    comparisons = {item["name"]: item for item in evaluation["metrics"]}
    assert comparisons["token_cost"]["favorable_delta"] > 0
    assert comparisons["token_cost"]["passed"] is True
    assert comparisons["task_success"]["threshold_met"] is True
    assert comparisons["task_success"]["non_regression_met"] is False

    evolution = evidence["evolution"]
    assert evolution["status"] == "rejected"
    assert evolution["rejection_reason"] == "offline_eval_failed"
    assert evolution["active_version"] == evolution["baseline_version"]

    memory_log = output / memory["store_path"]
    evolution_log = output / evolution["store_path"]
    memory_actions = [json.loads(line)["action"] for line in memory_log.read_text(encoding="utf-8").splitlines()]
    evolution_actions = [
        json.loads(line)["action"] for line in evolution_log.read_text(encoding="utf-8").splitlines()
    ]
    assert {"propose", "approve", "reject"} <= set(memory_actions)
    assert {"initialize", "submit", "offline_gate"} <= set(evolution_actions)
