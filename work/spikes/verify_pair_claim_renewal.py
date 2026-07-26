from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from crazy_harness.control_plane.store import SQLiteEventStore

with TemporaryDirectory() as root:
    store = SQLiteEventStore(f"{root}/claims.db")
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    claim = store.claim_work(claim_keys=("pair",), owner_id="first", ttl_seconds=2, now=now)
    assert claim and store.renew_work_claims(
        claims=claim, owner_id="first", ttl_seconds=2, now=now + timedelta(seconds=1)
    )
    blocked = store.claim_work(
        claim_keys=("pair",), owner_id="second", ttl_seconds=2,
        now=now + timedelta(seconds=2, milliseconds=500),
    )
    assert blocked is None
    print("PASS: renewal extends the same fenced Pair claim")
