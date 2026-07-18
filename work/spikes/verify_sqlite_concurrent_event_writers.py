from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from crazy_harness.control_plane.store import SQLiteEventStore
from crazy_harness.core.events import Event

with TemporaryDirectory() as root:
    store = SQLiteEventStore(Path(root) / "events.db")
    barrier = Barrier(2)
    def write(worker: int) -> None:
        barrier.wait(timeout=1)
        for index in range(20):
            store.append(Event(run_id="run", task_id="task", type="probe", source=str(worker), payload={"index": index}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, range(2)))
    assert len(store.read_all()) == 40
print("sqlite_concurrent_writers=PASS events=40")
