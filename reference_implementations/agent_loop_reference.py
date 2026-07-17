from __future__ import annotations


def run_until_idle(self, task_id: str) -> None:
    from crazy_harness.core.events.models import Event, EventType
    from crazy_harness.core.models.base import ModelRequest
    from crazy_harness.core.prompts.pack import PromptPack

    for turn in range(self.config.max_turns):
        pack = PromptPack(
            role_section=f"You are {self.config.agent_id}.",
            agent_card_section="Thin MVP agent card.",
            task_brief_section=f"Task: {task_id}",
            runtime_manifest_section="No external side effects in this scaffold.",
            context_policy_section="Use recent events only.",
            tool_policy_section="Return structured actions.",
            artifact_schema_section="Return AgentAction JSON.",
        )
        messages = pack.compile_messages()
        prompt_hash = pack.prompt_hash()
        self.event_log.append(Event(type=EventType.MODEL_REQUESTED, task_id=task_id, source=self.config.agent_id, payload={"turn": turn, "prompt_hash": prompt_hash}))
        response = self.model.complete(
            ModelRequest(
                agent_id=self.config.agent_id,
                task_id=task_id,
                messages=messages,
                prompt_hash=prompt_hash,
                runtime_manifest_ref="inline_scaffold",
            )
        )
        self.event_log.append(Event(type=EventType.MODEL_COMPLETED, task_id=task_id, source=self.config.agent_id, payload=response.model_dump(mode="json")))
        if response.action.type == "stop":
            self.event_log.append(Event(type=EventType.STOP_DECIDED, task_id=task_id, source=self.config.agent_id, payload={"reason": response.action.reason}))
            return
    self.event_log.append(Event(type=EventType.STOP_DECIDED, task_id=task_id, source=self.config.agent_id, payload={"reason": "max_turns"}))
