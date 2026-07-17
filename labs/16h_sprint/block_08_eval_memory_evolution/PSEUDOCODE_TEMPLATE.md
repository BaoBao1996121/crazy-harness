# 伪代码模板

```text
candidate = typed_diff + rationale + evidence
if candidate expands permission or changes hard policy:
    ______

offline = compare(baseline, candidate, scenario_rubrics)
if missing_metric or quality_regression:
    ______

shadow = run_without_side_effect_promotion()
if shadow passed:
    require ______
    promote versioned candidate

on regression:
    ______
```
