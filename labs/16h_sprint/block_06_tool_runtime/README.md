# Block 6: Tool Pipeline、Policy、Hooks 与 Runtime

一句话结论：原生 tool calling 只是候选动作入口，真实副作用之前仍要经历 schema、Hook 后重校验、授权、调度、隔离和记账。

## 运行

```powershell
python labs\16h_sprint\block_06_tool_runtime\run_demo.py
python -m pytest -q labs\16h_sprint\block_06_tool_runtime\fault_check.py
```

## 代码地图

- `core/tools/pipeline.py`: validate -> hook -> revalidate -> policy -> ledger -> execute。
- `core/tools/concurrency.py`: 只并发连续安全段，不跨写屏障重排。
- `core/runtime/local.py`: 受控主机命令；明确不是 sandbox。
- `core/runtime/browser.py`: screenshot/DOM/console/network 证据。
- `core/capabilities/`: Skill、Function、MCP 与 Tool Search 的渐进披露。

## 准出

- 画出完整 Tool Pipeline。
- 解释 Hook 可以 patch 参数但不能扩权，且 patch 后必须重校验。
- 区分 `is_read_only`、`is_concurrency_safe` 与 `is_destructive`。
