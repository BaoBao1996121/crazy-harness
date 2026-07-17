# Bug Card: 模型响应被重复调用

## 现象

`model.completed` 已经存在，进程重启后模型调用计数仍增加一次。

## 不变量

已持久化且可通过 schema 校验的响应属于事实；恢复时必须复用，不能再次采样模型。

## 诊断顺序

1. 找最后一个带 `turn_id` 的未完成 turn。
2. 检查 `model.completed` 是否存在。
3. 检查该 turn 是否已有终止事件。
4. 比较恢复前后的 provider call count。

不要先看正式实现；先让 `fault_check.py` 通过，再与 `AgentLoop._recoverable_turn` 对照。
