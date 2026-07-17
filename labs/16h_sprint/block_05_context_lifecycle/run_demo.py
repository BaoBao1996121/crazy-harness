from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]
SCENARIO = Path(__file__).with_name("scenario.py")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Block 5 scenario, then its tests.")
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
            "tests/core/test_microcompact.py",
            "tests/core/test_context_lifecycle.py",
            "labs/16h_sprint/block_05_context_lifecycle/test_block_05_scenario.py",
        ],
        cwd=ROOT,
        check=False,
    )
    return tests.returncode


if __name__ == "__main__":
    raise SystemExit(main())
