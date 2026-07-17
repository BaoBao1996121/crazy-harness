from crazy_harness.core.prompts import PromptPack, RuntimeManifest


def test_prompt_pack_compiles_messages_and_hash():
    manifest = RuntimeManifest(agent_id="coordinator", task_id="t1", mode="llm-dry")
    pack = PromptPack(
        role_section="You are Coordinator.",
        agent_card_section="capabilities: route",
        task_brief_section="Release the toy service.",
        runtime_manifest=manifest,
    )

    messages, prompt_hash = pack.compile()

    assert len(messages) == 2
    assert len(prompt_hash) == 64
    assert "Coordinator" in messages[0].content
