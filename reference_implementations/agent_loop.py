from __future__ import annotations

import json

from crazy_harness.core.agents.actions import AgentAction
from crazy_harness.core.events import Event
from crazy_harness.core.tools import ToolCall


def run_once(self) -> None:
    response = self.model.complete([])
    action = AgentAction.model_validate(json.loads(response.content))
    latest = self.event_log.read_all()[-1]
    self.event_log.append(
        Event(run_id=latest.run_id, task_id=latest.task_id, type="agent.action", source=self.agent_id, payload=action.model_dump())
    )
    if action.type == "call_tool":
        result = self.tool_registry.call(ToolCall(name=action.tool_name or "", args=action.tool_args))
        self.event_log.append(
            Event(run_id=latest.run_id, task_id=latest.task_id, type="tool.result", source=result.name, payload=result.model_dump())
        )
    elif action.type == "stop":
        self.event_log.append(
            Event(run_id=latest.run_id, task_id=latest.task_id, type="agent.stop", source=self.agent_id, payload={"reason": action.reason})
        )


def run_until_stop(self, *, max_steps=20) -> None:
    for _ in range(max_steps):
        self.run_once()
        if self.event_log.read_all()[-1].type == "agent.stop":
            return
