stages = {
    "evidence": set(),
    "risk": set(),
    "artifact": {"evidence", "risk"},
    "review": {"artifact"},
}

def ready(completed: set[str], active: set[str]) -> list[str]:
    return sorted(name for name, deps in stages.items() if name not in completed | active and deps <= completed)

assert ready(set(), set()) == ["evidence", "risk"]
assert ready({"evidence"}, {"risk"}) == []
assert ready({"evidence", "risk"}, set()) == ["artifact"]
assert ready({"evidence", "risk", "artifact"}, set()) == ["review"]
print("plan_dag_readiness=pass")
