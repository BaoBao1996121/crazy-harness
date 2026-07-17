scope_rank = {"global": 1, "project": 2, "agent": 3}
candidates = [
    {"scope": "project", "priority": 0, "source_id": "project-z"},
    {"scope": "agent", "priority": 0, "source_id": "agent-a"},
    {"scope": "agent", "priority": 1, "source_id": "agent-b"},
]
winner = max(
    candidates,
    key=lambda item: (scope_rank[item["scope"]], item["priority"], item["source_id"]),
)
assert winner["source_id"] == "agent-b"
print("skill_scope_precedence=ok")
