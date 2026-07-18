from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from crazy_harness.control_plane.runtime import ResidentRuntime, TaskRequest
from crazy_harness.core.models import FakeModelProvider, ModelResponse
from crazy_harness.taskpacks import ResidentDemoTeamTaskPack


class DeterministicDeepSeekContractProvider(FakeModelProvider):
    """Repeatable transport substitute; runtime governance remains fully active."""

    def __init__(self, responses: list[str], *, agent_run_id: str) -> None:
        super().__init__(responses)
        self.agent_run_id = agent_run_id

    def complete(self, messages, **kwargs) -> ModelResponse:
        response = super().complete(messages, **kwargs)
        return response.model_copy(
            update={
                "usage": {
                    "prompt_tokens": 108,
                    "prompt_cache_hit_tokens": 24,
                    "prompt_cache_miss_tokens": 84,
                    "completion_tokens": 20,
                    "total_tokens": 128,
                },
                "provider_response_id": f"demo-{self.agent_run_id}-{self.call_count}",
                "provider_model": "deepseek-v4-flash",
                "finish_reason": "stop",
            }
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a governed Team demo database.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("runs/control_plane_v07_governance"),
    )
    args = parser.parse_args()
    pack = ResidentDemoTeamTaskPack()

    def factory(binding):
        responses = (
            pack.scripted_peer_responses()
            if binding.agent_run_kind == "peer"
            else pack.scripted_assignment_responses(binding.stage_id)
        )
        return DeterministicDeepSeekContractProvider(
            responses,
            agent_run_id=binding.agent_run_id,
        )

    # submit_task only accepts DeepSeek mode when a credential is configured. No
    # network request is made because every child AgentRun receives the provider above.
    os.environ.setdefault("DEEPSEEK_API_KEY", "deterministic-contract-demo")
    runtime = ResidentRuntime(args.data_dir, team_model_factory=factory)
    created = runtime.submit_task(
        TaskRequest(
            title="持久模型治理演示 / Durable model governance",
            brief=(
                "让常驻 Agent Team 收集证据、对账、产出并独立审查，同时展示每次"
                "模型调用的预约、尝试、核销与运行级预算。"
            ),
            execution_mode="team",
            model_mode="deepseek",
        )
    )
    steps = runtime.run_until_idle(max_steps=180)
    snapshot = runtime.snapshot(created.run_id)
    print(
        json.dumps(
            {
                "data_dir": str(args.data_dir.resolve()),
                "run_id": created.run_id,
                "status": snapshot["run"]["status"],
                "steps": steps,
                "model_budget": snapshot["model_budget"],
                "model_calls": len(snapshot["model_calls"]),
                "evidence": "deterministic-contract-demo; no paid API request",
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
