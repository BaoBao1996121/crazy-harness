def recovery_action(operation_state: str) -> str:
    """Intentionally faulty external-effect recovery decision."""

    if operation_state == "unknown":
        return "retry"
    return "continue"
