from __future__ import annotations


class StopPolicy:
    """Thin stop policy for MVP-0.

    The hand-written AgentLoop should call this instead of deciding inline.
    """

    def should_stop(self, *, final_status: str | None, pending_events: int) -> bool:
        if final_status in {"approved", "rejected", "needs_human"}:
            return pending_events == 0
        return False
