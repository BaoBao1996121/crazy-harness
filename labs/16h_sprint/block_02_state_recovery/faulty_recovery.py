def should_reuse_persisted_response(event_types: list[str]) -> bool:
    """Intentionally faulty learning implementation."""

    return "model.completed" in event_types
