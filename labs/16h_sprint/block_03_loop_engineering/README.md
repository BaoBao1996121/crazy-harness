# Block 3: Goal、Exit Criteria 与 Loop Engineering

一句话结论：Agent 说 done 不是准出，Harness 必须机械检查 schema、证据和未决操作，再决定提交或有预算地 nudge。

## 运行

```powershell
python labs\16h_sprint\block_03_loop_engineering\run_demo.py
python -m pytest -q labs\16h_sprint\block_03_loop_engineering\fault_check.py
```

## 代码地图

- `core/agents/contracts.py`: Coordinator 交付的不可变 AssignmentContract。
- `core/agents/planning.py`: Agent 自己维护的 LocalPlan 事实与投影。
- `core/agents/completion.py`: CompletionGate、ProgressDetector、NudgeBudget。

## 准出

- 能区分 Goal、Exit Criteria、LocalPlan、Evidence 和 Nudge。
- 能解释计划全部完成为何仍可能 gate 失败。
- 能写出 repeated action 与 evidence delta 的判断。
