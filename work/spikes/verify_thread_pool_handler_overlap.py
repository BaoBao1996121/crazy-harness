from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic, sleep

barrier = Barrier(2)

def work(_: int) -> None:
    barrier.wait(timeout=1)
    sleep(0.15)

started = monotonic()
with ThreadPoolExecutor(max_workers=2) as pool:
    list(pool.map(work, range(2)))
elapsed = monotonic() - started
assert elapsed < 0.28, elapsed
print(f"thread_pool_overlap=PASS elapsed={elapsed:.3f}s")
