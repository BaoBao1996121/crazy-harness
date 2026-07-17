# Block 5: Context 编译、Offloading 与 Compact

一句话结论：Context 是每轮从事实源编译的临时视图；offload 保存原文，microcompact 每轮整理表示，full compact 才生成九维语义摘要。

## 运行

```powershell
python labs\16h_sprint\block_05_context_lifecycle\run_demo.py
python -m pytest -q labs\16h_sprint\block_05_context_lifecycle\fault_check.py
```

## 代码地图

- `core/context/builder.py`: 每轮重建与 ContextManifest。
- `core/context/microcompact.py`: discard/offload/inline 与 hydration lease。
- `core/context/compact.py`: 成对边界和九维 Full Compact 制品。
- `core/context/history.py`: 按 principal/assignment 授权召回。

## 准出

- 能区分 Offloading、Microcompact、阈值触发与 Full Compact。
- 能解释 read-full 只租用一轮，下一轮为何重新引用化。
- 能说明 compact 改 active representation，不删除 EventLog/Artifact。
