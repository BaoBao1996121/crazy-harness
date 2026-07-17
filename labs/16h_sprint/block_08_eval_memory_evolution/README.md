# Block 8: Trace、Replay、Eval、Memory 与 Evolution

一句话结论：最后不是再加一个 Agent，而是用证据判断机制是否真的提升，并让记忆和演进候选经过可回滚的门禁。

## 运行

```powershell
python labs\16h_sprint\block_08_eval_memory_evolution\run_demo.py
python -m pytest -q labs\16h_sprint\block_08_eval_memory_evolution\fault_check.py
```

## 代码地图

- `core/replay/replay.py`: dry replay 默认不重放副作用。
- `core/evals/`: baseline-vs-candidate、缺指标 fail-closed。
- `core/memory/`: typed candidate、证据、冲突、人工 approve/reject/revoke。
- `core/evals/evolution.py`: offline -> shadow -> human approval -> promote/rollback。

## 准出

- 能从 EventLog、Agent Trace、Eval Report 三层定位问题。
- 能解释长期记忆为什么先是 candidate，不自动进入 Context。
- 能解释 token 更少为何不等于演进更好，以及如何拒绝负提升。
