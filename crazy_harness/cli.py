from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="crazy")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="Run a resident team loop.")
    run.add_argument("world", choices=["dev-release"])
    run.add_argument("--mode", choices=["mock", "llm-dry", "llm-live"], default="mock")
    run.add_argument("--team", action="store_true", help="Run the resident Scout/Builder/Reviewer team.")
    run.add_argument("--repo", default="examples/hello-crazy-api")
    run.add_argument("--runs-dir", default="runs")

    replay = subparsers.add_parser("replay", help="Replay an events.jsonl file.")
    replay.add_argument("events_jsonl")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "run":
        if args.team:
            if args.mode != "mock":
                raise SystemExit("--team currently supports --mode mock; use single-agent mode for live LLM smoke")
            from crazy_harness.worlds.cicd.team import build_dev_release_team_runtime

            runtime = build_dev_release_team_runtime(
                repo_path=Path(args.repo),
                runs_dir=Path(args.runs_dir),
            )
            print(runtime.run())
            return 0

        from crazy_harness.worlds.cicd.world import build_dev_release_runtime

        runtime = build_dev_release_runtime(
            mode=args.mode,
            repo_path=Path(args.repo),
            runs_dir=Path(args.runs_dir),
        )
        print(runtime.run())
        return 0

    if args.command == "replay":
        from crazy_harness.core.replay.replay import replay_events

        replay_events(Path(args.events_jsonl))
        return 0

    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
