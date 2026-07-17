from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict, Field

from crazy_harness.core.events import Event


class HistoryNotFound(LookupError):
    pass


class HistoryAccessDenied(PermissionError):
    pass


class HistoryPrincipal(BaseModel):
    model_config = ConfigDict(frozen=True)

    principal: str = Field(min_length=1)
    assignment_id: str = Field(min_length=1)


class HistoryACL(BaseModel):
    model_config = ConfigDict(frozen=True)

    grants: tuple[HistoryPrincipal, ...] = Field(default_factory=tuple)

    def allows(self, *, owner: HistoryPrincipal, subject: HistoryPrincipal) -> bool:
        return subject == owner or subject in self.grants


class HistoryRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    ref: str = Field(min_length=1)
    owner: HistoryPrincipal
    event: Event
    acl: HistoryACL = Field(default_factory=HistoryACL)


class HistoryService:
    """Authorized lookup over registered history refs, never arbitrary paths."""

    def __init__(self, records: Iterable[HistoryRecord] = ()) -> None:
        self._records: dict[str, HistoryRecord] = {}
        for record in records:
            self.register(record)

    def register(self, record: HistoryRecord) -> None:
        if record.ref in self._records:
            raise ValueError(f"history ref already registered: {record.ref}")
        self._records[record.ref] = record

    def read(
        self,
        ref: str,
        *,
        subject: HistoryPrincipal | None = None,
        principal: str | None = None,
        assignment_id: str | None = None,
    ) -> HistoryRecord:
        requester = _requester(subject, principal, assignment_id)
        record = self._records.get(ref)
        if record is None:
            raise HistoryNotFound(f"unknown history ref: {ref}")
        if not record.acl.allows(owner=record.owner, subject=requester):
            raise HistoryAccessDenied("principal is not allowed to read this assignment history")
        return record

    def search(
        self,
        keyword: str,
        *,
        subject: HistoryPrincipal | None = None,
        principal: str | None = None,
        assignment_id: str | None = None,
        limit: int = 20,
    ) -> list[HistoryRecord]:
        requester = _requester(subject, principal, assignment_id)
        needle = keyword.strip().casefold()
        if not needle:
            raise ValueError("keyword must not be empty")
        if limit < 1:
            raise ValueError("limit must be at least one")

        matches: list[HistoryRecord] = []
        for record in self._records.values():
            if not record.acl.allows(owner=record.owner, subject=requester):
                continue
            searchable = f"{record.ref}\n{record.event.model_dump_json()}".casefold()
            if needle in searchable:
                matches.append(record)
                if len(matches) == limit:
                    break
        return matches


def _requester(
    subject: HistoryPrincipal | None,
    principal: str | None,
    assignment_id: str | None,
) -> HistoryPrincipal:
    if subject is not None:
        if principal is not None or assignment_id is not None:
            raise ValueError("pass either subject or principal plus assignment_id")
        return subject
    if principal is None or assignment_id is None:
        raise ValueError("principal and assignment_id are required")
    return HistoryPrincipal(principal=principal, assignment_id=assignment_id)
