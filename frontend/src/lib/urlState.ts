export function mergeSearchParam(
  search: string,
  name: "eval" | "run",
  value: string | undefined,
): string {
  const params = new URLSearchParams(search);
  if (value) params.set(name, value);
  else params.delete(name);
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

interface SearchParamStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

export function restoreSearchParam(
  search: string,
  name: "eval" | "run",
  storageKey: string,
  storage: SearchParamStorage,
): string | undefined {
  const requested = new URLSearchParams(search).get(name)?.trim();
  if (requested) {
    storage.setItem(storageKey, requested);
    return requested;
  }
  return storage.getItem(storageKey)?.trim() || undefined;
}
