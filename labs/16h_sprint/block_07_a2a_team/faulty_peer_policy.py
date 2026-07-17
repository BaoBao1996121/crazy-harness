def authorize_peer(depth: int, max_depth: int, requested_scope: set[str], allowed_scope: set[str]) -> bool:
    """Intentionally faulty: scope is checked but hop depth is ignored."""

    return requested_scope <= allowed_scope
