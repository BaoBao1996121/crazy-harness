from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from crazy_harness.control_plane.api import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Crazy Resident A2A Control Plane")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path("runs/control_plane"))
    args = parser.parse_args()
    uvicorn.run(create_app(args.data_dir), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
