from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from crazy_harness.control_plane.api import create_app


parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
with tempfile.TemporaryDirectory() as temp_dir:
    schema = create_app(Path(temp_dir), background=False).openapi()
args.output.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"openapi={args.output}")
