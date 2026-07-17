# Block 1：Agent Loop 运行与控制权

## 目标

不是背 Python，而是看清一次模型建议怎样变成 Harness 事实。

## 运行

```powershell
cd path\to\crazy-harness
python labs\16h_sprint\block_01_agent_loop\run_demo.py
```

然后依次查看：

1. `runs/learning_block_01/naive_trace.json`
2. `runs/learning_block_01/known_good_runs/<run_id>/events.jsonl`
3. 同目录的 `report.md`

## 阅读代码顺序

1. `naive_loop.py`
2. `crazy_harness/core/agents/actions.py`
3. `crazy_harness/core/agents/state.py`
4. `crazy_harness/core/agents/loop.py`
5. `tests/core/test_agent_loop_recovery.py`

## 准出

- 能指出 naive loop 中五个隐含 phase。
- 能画出 Model、Harness、Tool、EventLog 的控制权关系。
- 能完成 `PSEUDOCODE_TEMPLATE.md`。
- 能预测三个 crash marker 的重复行为。
