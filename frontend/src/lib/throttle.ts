export interface AsyncThrottle<T> {
  schedule: (value: T) => void;
  cancel: () => void;
}

export function createAsyncThrottle<T>(
  task: (value: T) => Promise<void>,
  intervalMs: number,
): AsyncThrottle<T> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  let latest: T;
  let hasLatest = false;
  let running = false;
  let cancelled = false;

  const arm = () => {
    if (cancelled || running || timer !== null || !hasLatest) return;
    timer = setTimeout(() => {
      timer = null;
      void flush();
    }, intervalMs);
  };

  const flush = async () => {
    if (cancelled || running || !hasLatest) return;
    const value = latest;
    hasLatest = false;
    running = true;
    try {
      await task(value);
    } finally {
      running = false;
      arm();
    }
  };

  return {
    schedule(value) {
      if (cancelled) return;
      latest = value;
      hasLatest = true;
      arm();
    },
    cancel() {
      cancelled = true;
      hasLatest = false;
      if (timer !== null) clearTimeout(timer);
      timer = null;
    },
  };
}
