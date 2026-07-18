import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from tempfile import TemporaryDirectory
from threading import Barrier
with TemporaryDirectory() as root:
    path, barrier = f"{root}/budget.db", Barrier(2)
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE budget (run_id TEXT PRIMARY KEY, remaining INTEGER); INSERT INTO budget VALUES ('run-1', 100);")
    def reserve(_: int) -> int:
        with closing(sqlite3.connect(path, timeout=5, isolation_level=None)) as db:
            barrier.wait()
            db.execute("BEGIN IMMEDIATE")
            won = db.execute("UPDATE budget SET remaining=remaining-60 WHERE run_id='run-1' AND remaining>=60").rowcount
            db.commit()
            return won
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [0, 1]
print("sqlite_atomic_model_budget=PASS remaining=40")
