import os
from pathlib import Path
from tempfile import TemporaryDirectory


with TemporaryDirectory() as root:
    path = Path(root) / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"model.completed"}\n')
        handle.flush()
        os.fsync(handle.fileno())
    assert path.read_text(encoding="utf-8").endswith("\n")
    print("windows_fsync=ok")
