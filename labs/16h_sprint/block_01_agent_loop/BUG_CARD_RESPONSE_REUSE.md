# Bug Card：恢复时重复调用模型

## 现象

在 `after_model_persisted` 注入崩溃后重启，模型调用计数从 1 变成 2。

## 调试约束

- 先运行 `tests/core/test_agent_loop_recovery.py::test_recovery_reuses_persisted_model_response`。
- 不先搜索答案，先找最后一个 `model.completed`。
- 判断当前 turn 是否已经有 `agent.command.validated`。
- 修复范围应集中在 recoverable turn 的选择，不改 Provider。

## 通过证据

- 重启前后 `FakeModelProvider.call_count == 1`。
- 原 response 被复用并产生 `tool.completed`。
- EventLog 中不存在第二个 `model.requested`。
