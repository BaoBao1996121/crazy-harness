def authorize_after_hook(original_path: str, patched_path: str) -> bool:
    """Intentionally faulty: only the pre-hook value is validated."""

    return not original_path.startswith("..")
