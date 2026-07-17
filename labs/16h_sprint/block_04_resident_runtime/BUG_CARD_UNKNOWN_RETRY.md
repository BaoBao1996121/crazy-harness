# Bug Card: UNKNOWN 被当成失败重试

## 现象

外部操作可能已生效但结果未落盘，恢复逻辑直接再次执行，产生重复副作用。

## 不变量

`UNKNOWN` 不是 `FAILED`。先用业务侧事实对账，再进入 `SUCCEEDED`、`FAILED` 或继续 `UNKNOWN`。

## 诊断提示

分别标出 EventLog 的最后可信事件与 OperationLedger 的状态。不要用“工具抛异常”推断“外部世界没有变化”。
