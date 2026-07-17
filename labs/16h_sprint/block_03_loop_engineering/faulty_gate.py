def can_submit(output: dict, evidence: dict[str, list[str]], pending: list[str]) -> bool:
    """Intentionally faulty: it trusts shape and ignores proof/state."""

    return isinstance(output.get("risk_level"), str)
