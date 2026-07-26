from uuid import NAMESPACE_URL, uuid5


def child_request_id(campaign_id: str, trial_index: int) -> str:
    value = uuid5(NAMESPACE_URL, f"crazy:campaign:{campaign_id}:trial:{trial_index}")
    return f"campaign-trial:{value.hex}"


first = [child_request_id("campaign_demo", index) for index in range(1, 31)]
second = [child_request_id("campaign_demo", index) for index in range(1, 31)]
assert first == second
assert len(first) == len(set(first))
assert max(map(len, first)) <= 128
print("child_pair_identity=replay_stable; unique_trials=30")
