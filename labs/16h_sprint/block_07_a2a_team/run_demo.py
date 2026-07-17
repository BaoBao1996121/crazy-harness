from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/core/test_a2a_teamwork.py", "tests/e2e/test_dev_release_team_mock.py"],
        cwd=ROOT,
        check=False,
    ).returncode
)
