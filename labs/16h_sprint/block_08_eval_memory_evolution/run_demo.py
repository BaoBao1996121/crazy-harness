from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = Path(__file__).with_name("scenario.py")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Block 8 scenario, then its tests.")
    parser.add_argument("--output", type=Path, help="Scenario evidence directory.")
    args = parser.parse_args()

    scenario_command = [sys.executable, str(SCENARIO)]
    if args.output is not None:
        scenario_command.extend(["--output", str(args.output)])
    scenario = subprocess.run(scenario_command, cwd=ROOT, check=False)
    if scenario.returncode != 0:
        return scenario.returncode

    tests = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/core/test_replay.py",
            "tests/core/test_memory_eval_evolution.py",
            "labs/16h_sprint/block_08_eval_memory_evolution/test_block_08_scenario.py",
        ],
        cwd=ROOT,
        check=False,
    )
    return tests.returncode


if __name__ == "__main__":
    raise SystemExit(main())
