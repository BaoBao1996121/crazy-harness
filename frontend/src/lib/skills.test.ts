import { describe, expect, it } from "vitest";

import type { EventRecord } from "./events";
import { skillViewFromEvents } from "./skills";

describe("skillViewFromEvents", () => {
  it("joins the latest body-free catalog with durable activation metadata", () => {
    const body = "PRIVATE SKILL BODY";
    const records = [
      {
        cursor: 1,
        event: {
          id: "catalog-event",
          run_id: "run-1",
          task_id: "task-1",
          type: "skill.catalog.compiled",
          source: "taskpack",
          payload: {
            disclosure: "metadata_then_explicit_activation",
            manifest_hash: "manifest-hash",
            entries: [{ name: "repo-review", description: "Review evidence", scope: "project", source_id: "project" }],
            diagnostics: [{ code: "skill_shadowed", severity: "warning", source_id: "global", skill_name: "repo-review", message: "shadowed" }],
          },
        },
      },
      {
        cursor: 2,
        event: {
          id: "activation-event",
          run_id: "run-1",
          task_id: "task-1",
          type: "tool.completed",
          source: "generalist",
          payload: {
            result: {
              name: "skill.activate",
              status: "ok",
              output: JSON.stringify({
                name: "repo-review",
                scope: "project",
                source_id: "project",
                body,
                body_hash: "body-hash",
                source_hash: "source-hash",
                resources: ["references/checklist.md"],
                allowed_tools_hint: ["repo.read"],
              }),
            },
          },
        },
      },
    ] as EventRecord[];

    const state = skillViewFromEvents(records);

    expect(state.catalog).toEqual([
      { name: "repo-review", description: "Review evidence", scope: "project", sourceId: "project" },
    ]);
    expect(state.active[0]).toMatchObject({
      name: "repo-review",
      bodyChars: body.length,
      resourceCount: 1,
      allowedToolsHint: ["repo.read"],
      eventId: "activation-event",
    });
    expect(state.diagnostics[0].code).toBe("skill_shadowed");
    expect(JSON.stringify(state)).not.toContain(body);
  });

  it("ignores malformed activation output", () => {
    const records = [{
      cursor: 1,
      event: {
        id: "bad",
        run_id: "run-1",
        task_id: "task-1",
        type: "tool.completed",
        source: "generalist",
        payload: { result: { name: "skill.activate", status: "ok", output: "not-json" } },
      },
    }] as EventRecord[];

    expect(skillViewFromEvents(records).active).toEqual([]);
  });
});
