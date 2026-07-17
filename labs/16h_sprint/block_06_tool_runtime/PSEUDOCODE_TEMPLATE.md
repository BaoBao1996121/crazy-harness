# 伪代码模板

```text
candidate = normalize_native_tool_call(model_output)
spec = capability_catalog.resolve(candidate.name)
validate(candidate.args, spec.schema)
patched = hooks.pre_tool(candidate)
______
policy.require(patched, assignment_authority)

batches = group_consecutive_safe_calls_without_crossing_write_barrier()
for batch in batches:
    ledger.plan(idempotency_key)
    ledger.start()
    execute in ______
    persist ______ or UNKNOWN
```
