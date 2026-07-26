from hashlib import sha256


def indices(seed: str, size: int, round_index: int) -> tuple[int, ...]:
    return tuple(
        int.from_bytes(sha256(f"{seed}:{round_index}:{slot}".encode()).digest()[:8], "big") % size
        for slot in range(size)
    )


pairs = ((0, 10), (1, 11), (2, 12), (3, 13))
first = tuple(indices("campaign-contract", len(pairs), turn) for turn in range(100))
second = tuple(indices("campaign-contract", len(pairs), turn) for turn in range(100))
assert first == second
assert all(len([pairs[index] for index in sample]) == len(pairs) for sample in first)
print("paired_resampling=deterministic; same_indices_for_both_arms=true")
