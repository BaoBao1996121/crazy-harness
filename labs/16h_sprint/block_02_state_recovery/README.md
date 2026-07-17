# Block 2: 显式状态与崩溃恢复

一句话结论：恢复不是“重跑函数”，而是从最后一个可信事件判断下一步，并复用已经持久化的模型响应。

## 运行

```powershell
python labs\16h_sprint\block_02_state_recovery\run_demo.py
python -m pytest -q labs\16h_sprint\block_02_state_recovery\fault_check.py
```

第二条命令初始应失败；先从断言、事件序列和模型调用次数提出假设，再只修改 `faulty_recovery.py`。

## 代码地图

- `crazy_harness/core/agents/loop.py`: phase、response reuse、operation recovery。
- `crazy_harness/core/agents/state.py`: 合法状态迁移。
- `tests/core/test_agent_loop_recovery.py`: 三个 crash marker 的证据。

## 准出

- 画出五个核心 phase，并标记可信持久化点。
- 解释 response recovery 与外部 effect recovery 为何不是同一问题。
- 写出 `response 已落盘但本轮未完成 -> 不再调模型` 的伪代码。
