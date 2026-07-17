import json
import sqlite3
import tempfile
from pathlib import Path

path = Path(tempfile.mkdtemp()) / "events.db"
with sqlite3.connect(path) as db:
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL)")
    db.execute("INSERT INTO events(payload) VALUES (?)", (json.dumps({"type": "seed"}),))
with sqlite3.connect(path) as db:
    db.execute("INSERT INTO events(payload) VALUES (?)", (json.dumps({"type": "wake"}),))
    rows = db.execute("SELECT seq, payload FROM events ORDER BY seq").fetchall()
assert [row[0] for row in rows] == [1, 2]
assert [json.loads(row[1])["type"] for row in rows] == ["seed", "wake"]
print("sqlite_cursor_reopen=ok")
