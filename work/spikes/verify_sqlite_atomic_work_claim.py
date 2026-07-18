import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

with TemporaryDirectory() as root:
    path, barrier = Path(root) / "claims.db", Barrier(2)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE claims (claim_key TEXT PRIMARY KEY, owner TEXT)")
    def claim(owner: str) -> int:
        with closing(sqlite3.connect(path, timeout=5)) as connection:
            barrier.wait(timeout=1)
            with connection:
                return connection.execute("INSERT OR IGNORE INTO claims VALUES ('delivery-1', ?)", (owner,)).rowcount
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ("one", "two"))) == [0, 1]
print("sqlite_atomic_work_claim=PASS winners=1")
