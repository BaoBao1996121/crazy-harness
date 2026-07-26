export type IdentityParam = "campaign" | "eval" | "run" | "trial";

const IDENTITY_PARAMS: IdentityParam[] = ["campaign", "eval", "run", "trial"];

export function mergeSearchParam(
  search: string,
  name: IdentityParam,
  value: string | undefined,
): string {
  const params = new URLSearchParams(search);
  if (value) params.set(name, value);
  else params.delete(name);
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

export function replaceIdentitySelection(
  search: string,
  selection: Partial<Record<IdentityParam, string | undefined>>,
): string {
  const params = new URLSearchParams(search);
  for (const name of IDENTITY_PARAMS) params.delete(name);
  for (const name of IDENTITY_PARAMS) {
    const value = selection[name]?.trim();
    if (value) params.set(name, value);
  }
  const serialized = params.toString();
  return serialized ? `?${serialized}` : "";
}

export function hasExplicitIdentity(search: string): boolean {
  const params = new URLSearchParams(search);
  return IDENTITY_PARAMS.some((name) => Boolean(params.get(name)?.trim()));
}

export function resolveIdentityParam(
  search: string,
  name: IdentityParam,
  storedValue: string | null,
): string | undefined {
  const requested = new URLSearchParams(search).get(name)?.trim();
  if (requested) return requested;
  if (hasExplicitIdentity(search)) return undefined;
  return storedValue?.trim() || undefined;
}

interface SearchParamStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

export function restoreSearchParam(
  search: string,
  name: IdentityParam,
  storageKey: string,
  storage: SearchParamStorage,
): string | undefined {
  const requested = new URLSearchParams(search).get(name)?.trim();
  if (requested) {
    storage.setItem(storageKey, requested);
    return requested;
  }
  return resolveIdentityParam(search, name, storage.getItem(storageKey));
}
