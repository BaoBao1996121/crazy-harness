from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/core/test_loop_engineering.py"],
        cwd=ROOT,
        check=False,
    ).returncode
)
