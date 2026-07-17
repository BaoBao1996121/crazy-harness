from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
raise SystemExit(
    subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/core/test_resident_runtime.py", "tests/core/test_tool_pipeline.py"],
        cwd=ROOT,
        check=False,
    ).returncode
)
