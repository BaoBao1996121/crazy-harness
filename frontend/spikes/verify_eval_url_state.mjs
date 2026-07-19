import { strict as assert } from "node:assert";

const url = new URL("http://localhost/?run=run_single&eval=eval_pair");
url.searchParams.set("run", "run_team");
assert.equal(url.searchParams.get("eval"), "eval_pair");
assert.equal(url.searchParams.get("run"), "run_team");
url.searchParams.delete("eval");
assert.equal(url.searchParams.get("run"), "run_team");
console.log("run and eval URL state coexist: ok");
