from crazy_harness.core.a2a import AgentCard

cards = (
    AgentCard(agent_id="scout", role="primary", capabilities=["evidence.collect", "peer.respond"]),
    AgentCard(agent_id="researcher", role="backup", capabilities=["evidence.collect"]),
    AgentCard(agent_id="builder", role="writer", capabilities=["artifact.compose"]),
)
required = {"evidence.collect"}

def choose(statuses: dict[str, str]) -> str:
    eligible = [card for card in cards if statuses[card.agent_id] == "idle" and required <= set(card.capabilities)]
    return min(eligible, key=lambda card: (len(set(card.capabilities) - required), card.agent_id)).agent_id

assert choose({"scout": "idle", "researcher": "busy", "builder": "idle"}) == "scout"
assert choose({"scout": "degraded", "researcher": "idle", "builder": "idle"}) == "researcher"
print("capability_selection=pass")
