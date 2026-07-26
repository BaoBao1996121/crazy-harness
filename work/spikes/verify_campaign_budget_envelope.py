from decimal import Decimal


def within_cap(trials: int, per_arm_tokens: int, per_arm_cost: str, token_cap: int, cost_cap: str) -> bool:
    return (
        2 * trials * per_arm_tokens <= token_cap
        and 2 * trials * Decimal(per_arm_cost) <= Decimal(cost_cap)
    )


assert within_cap(5, 250_000, "0.10", 2_500_000, "1.00")
assert not within_cap(6, 250_000, "0.10", 2_500_000, "1.00")
assert not within_cap(5, 250_000, "0.1000001", 2_500_000, "1.00")
print("campaign_budget=exact_decimal; oversubscription_rejected=true")
