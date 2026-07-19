import { readFileSync } from "node:fs";
import { strict as assert } from "node:assert";

const spec = JSON.parse(readFileSync(new URL("../openapi.json", import.meta.url), "utf8"));
assert.ok(spec.paths["/api/evals/pairs"].get);
assert.ok(spec.paths["/api/evals/pairs"].post);
assert.ok(spec.paths["/api/evals/pairs/{eval_id}"].get);
assert.ok(spec.paths["/api/evals/pairs/{eval_id}/drain"].post);
assert.ok(spec.components.schemas.PairedEvalReport);
console.log("paired eval OpenAPI contract: ok");
