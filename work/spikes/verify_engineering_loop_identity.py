from uuid import NAMESPACE_URL, uuid5


def identity(loop_id: str, iteration: int) -> tuple[str, str]:
    key = f"crazy:engineering-loop:{loop_id}:iteration:{iteration}"
    return (
        f"iteration_{uuid5(NAMESPACE_URL, key).hex[:16]}",
        f"run_{uuid5(NAMESPACE_URL, key + ':run').hex[:12]}",
    )


first = [identity("loop-demo", index) for index in range(1, 31)]
second = [identity("loop-demo", index) for index in range(1, 31)]
assert first == second
assert len(first) == len(set(first)) == 30
print("PASS: 30 deterministic iteration and child Run identities")
