import type { EventRecord } from "./events";

export interface SkillCatalogItem {
  name: string;
  description: string;
  scope: string;
  sourceId: string;
}

export interface SkillDiagnosticItem {
  code: string;
  severity: string;
  sourceId: string;
  skillName?: string;
  message: string;
}

export interface ActiveSkillItem {
  name: string;
  scope: string;
  sourceId: string;
  bodyHash: string;
  sourceHash: string;
  bodyChars: number;
  resourceCount: number;
  allowedToolsHint: string[];
  eventId: string;
}

export interface SkillViewState {
  catalog: SkillCatalogItem[];
  diagnostics: SkillDiagnosticItem[];
  active: ActiveSkillItem[];
  manifestHash: string;
  disclosure: string;
}

export function skillViewFromEvents(records: EventRecord[]): SkillViewState {
  const catalogEvent = [...records]
    .reverse()
    .find((record) => record.event.type === "skill.catalog.compiled");
  const catalogPayload = objectValue(catalogEvent?.event.payload);
  const catalog = arrayValue(catalogPayload.entries)
    .map(parseCatalogItem)
    .filter((item): item is SkillCatalogItem => item !== null);
  const diagnostics = arrayValue(catalogPayload.diagnostics)
    .map(parseDiagnostic)
    .filter((item): item is SkillDiagnosticItem => item !== null);

  const activeByName = new Map<string, ActiveSkillItem>();
  records.forEach((record) => {
    const activation = parseActivation(record);
    if (activation) activeByName.set(activation.name, activation);
  });

  return {
    catalog,
    diagnostics,
    active: [...activeByName.values()].sort((left, right) => left.name.localeCompare(right.name)),
    manifestHash: stringValue(catalogPayload.manifest_hash),
    disclosure: stringValue(catalogPayload.disclosure),
  };
}

function parseCatalogItem(value: unknown): SkillCatalogItem | null {
  const item = objectValue(value);
  const name = stringValue(item.name);
  const description = stringValue(item.description);
  if (!name || !description) return null;
  return {
    name,
    description,
    scope: stringValue(item.scope),
    sourceId: stringValue(item.source_id),
  };
}

function parseDiagnostic(value: unknown): SkillDiagnosticItem | null {
  const item = objectValue(value);
  const code = stringValue(item.code);
  if (!code) return null;
  const skillName = stringValue(item.skill_name);
  return {
    code,
    severity: stringValue(item.severity),
    sourceId: stringValue(item.source_id),
    ...(skillName ? { skillName } : {}),
    message: stringValue(item.message),
  };
}

function parseActivation(record: EventRecord): ActiveSkillItem | null {
  if (record.event.type !== "tool.completed") return null;
  const result = objectValue(objectValue(record.event.payload).result);
  if (result.name !== "skill.activate" || !["ok", "success", "succeeded"].includes(stringValue(result.status).toLowerCase())) {
    return null;
  }
  if (typeof result.output !== "string") return null;
  try {
    const output = objectValue(JSON.parse(result.output));
    const name = stringValue(output.name);
    const body = stringValue(output.body);
    if (!name || !body) return null;
    return {
      name,
      scope: stringValue(output.scope),
      sourceId: stringValue(output.source_id),
      bodyHash: stringValue(output.body_hash),
      sourceHash: stringValue(output.source_hash),
      bodyChars: body.length,
      resourceCount: arrayValue(output.resources).length,
      allowedToolsHint: arrayValue(output.allowed_tools_hint).filter(
        (item): item is string => typeof item === "string",
      ),
      eventId: record.event.id ?? "",
    };
  } catch {
    return null;
  }
}

function objectValue(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}
