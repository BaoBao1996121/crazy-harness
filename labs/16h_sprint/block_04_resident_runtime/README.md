# Block 4: 常驻 Runtime、Wait 与 UNKNOWN

一句话结论：常驻不是 Python 进程永不退出，而是邮箱、等待条件、任务状态和操作账本都可从磁盘恢复。

## 运行

```powershell
python labs\16h_sprint\block_04_resident_runtime\run_demo.py
python -m pytest -q labs\16h_sprint\block_04_resident_runtime\fault_check.py
```

## 代码地图

- `core/runtime/mailbox.py`: at-least-once 持久投递与 ack。
- `core/runtime/scheduler.py`: ready/busy/waiting 与事件唤醒。
- `core/runtime/state.py`: Agent、Assignment、Operation 三套状态投影。
- `core/tools/pipeline.py`: OperationLedger 与 reconciliation。

## 准出

- 解释 `schedule()` 与带消息的 `wake()` 为何要分开。
- 解释等待期间为何 LLM 调用为零。
- 能写出 `UNKNOWN -> RECONCILING -> terminal`，且不会直接 retry。
