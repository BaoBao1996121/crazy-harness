import { readFileSync } from "node:fs";
import { strict as assert } from "node:assert";

const spec = JSON.parse(readFileSync(new URL("../openapi.json", import.meta.url), "utf8"));
const schemas = spec.components.schemas;
const armRequired = new Set(schemas.PairedEvalArmReport.required);
const reportRequired = new Set(schemas.PairedEvalReport.required);
assert.equal(armRequired.has("score"), false);
assert.equal(armRequired.has("trace"), false);
assert.equal(reportRequired.has("recommendation"), false);
console.log("running eval permits progressive metrics: ok");
