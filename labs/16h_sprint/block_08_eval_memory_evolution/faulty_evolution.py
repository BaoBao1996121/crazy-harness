def should_promote(baseline_success: float, candidate_success: float, baseline_tokens: int, candidate_tokens: int) -> bool:
    """Intentionally faulty: it optimizes cost while ignoring task quality."""

    return candidate_tokens < baseline_tokens
