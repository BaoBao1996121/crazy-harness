export interface CapabilityRecallEntry {
  name: string;
  sourceEventId: string;
}

export function capabilityRecallEntries(value: unknown): CapabilityRecallEntry[] {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return [];
  }

  return Object.entries(value)
    .filter(
      (entry): entry is [string, string] =>
        entry[0].trim().length > 0 &&
        typeof entry[1] === "string" &&
        entry[1].trim().length > 0,
    )
    .map(([name, sourceEventId]) => ({ name, sourceEventId }))
    .sort((left, right) => left.name.localeCompare(right.name));
}
export interface CapabilityIdentityEntry {
  name: string;
  kind: string;
  provider: string;
}

export function capabilityIdentityEntries(
  names: string[],
  kindsValue: unknown,
  providersValue: unknown,
): CapabilityIdentityEntry[] {
  const kinds = stringRecord(kindsValue);
  const providers = stringRecord(providersValue);
  return names
    .filter((name) => name.trim().length > 0)
    .map((name) => ({
      name,
      kind: kinds[name] ?? "function",
      provider: providers[name] ?? "local",
    }));
}

function stringRecord(value: unknown): Record<string, string> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return {};
  }
  return Object.fromEntries(
    Object.entries(value).filter(
      (entry): entry is [string, string] =>
        entry[0].trim().length > 0 &&
        typeof entry[1] === "string" &&
        entry[1].trim().length > 0,
    ),
  );
}
