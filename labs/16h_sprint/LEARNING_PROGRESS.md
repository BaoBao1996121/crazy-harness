# Crazy Harness 16 小时学习进度

> 更新日期：2026-07-13
> 当前进度：Block 1、Block 2 概念准出通过；学习方式切换为全套高密度背诵与主动回忆

## 当前学习方式

为压缩时间，不再严格按八个 Block 逐块调试。当前使用四份材料：

1. `docs/HARNESS_DAILY_REVIEW_20260714.md`：当前理解背诵入口，包含今天答疑、Memory 八条、18 句背诵和自测。
2. `docs/HARNESS_CORE_ESSENTIALS.md`：六大 Harness 工程主线的浓缩参考。
3. `docs/HARNESS_ULTRA_CHEAT_SHEET.md`：需要快速补齐工程关键词时使用的超短卡。
4. `docs/HARNESS_INTERVIEW_CRAM_SHEET.md`：概念不理解或需要完整例子、伪代码、故障分析时查阅。

目标调整为先达到“能讲、能画、能写关键伪代码、知道真实边界”，完整源码调试作为后续巩固，不把背诵等同于独立生产经验。

## 总体理解

Harness 可以分成三层：

1. 通用可靠性底座：EventLog、显式状态机、幂等、Ledger、队列与沙箱。
2. LLM 信任边界：Structured Action、Command Validation、Tool Policy 与 CompletionGate。
3. Agent 认知支持：Context、Memory、Nudge、LocalPlan 与 Skill。

成熟软件工程机制本身并非 Agent 独有；当它们围绕概率型模型的调用、上下文、工具副作用和停止权组成控制系统时，就构成 Agent Harness。

## Block 1：Agent Loop 与控制权

状态：**准出通过**。

已掌握：

- 区分教学 `naive_loop`、正式 `AgentLoop` 与负责组装依赖的 `Runtime`。
- 理解 `Runtime.run -> run_until_stop -> run_once` 三个层级。
- 能沿 `Model -> Command -> Tool -> Observation -> Stop/Next Turn` 解释一轮循环。
- 模型只提出候选动作；Harness 校验、授权、执行、记录事实并保留停止权。
- `FakeModelProvider` 与 DeepSeek Provider 共用同一 AgentLoop，差异由 Runtime 注入。
- EventLog 不是普通调试日志，而是恢复、Context 和状态推导使用的事实源。

纠正过的误区：

- Tool Result 不是“消除所有幻觉”，而是不允许模型陈述直接成为外部事实。
- 当前 AgentLoop 主路径一次只传一个 ToolRequest；并发 Planner 已有组件测试，但批量 ToolCall 尚未端到端接入主循环。
- `operation.started + tool.requested` 没有 terminal Event 时，不能推断失败，也不能盲目重试。

面试记忆句：

> LLM 是候选动作生成器，不是事实源；Harness 才负责把动作转成经过校验、授权、执行和持久化的系统事实。

## Block 2：显式状态与崩溃恢复

状态：**概念准出通过，源码熟练度中等，后续需要间隔复习**。

已完成：

- 运行 known-good Recovery 测试。
- 修复 `faulty_recovery.py`：只要 `model.completed` 已持久化，就应复用 Response，不等待 `tool.completed`。
- 理解显式 Phase 用于观察和约束控制流，但恢复仍要检查语义事件。
- 区分 Response Recovery 与 External Effect Recovery。
- 理解非法 Command 必须在工具执行前失败，不能产生副作用。

核心决策：

| 最后可信事实 | 恢复动作 |
|---|---|
| `model.requested`，无 `model.completed` | 没有可恢复 Response，可以重新调用模型 |
| `model.completed`，无合法 Command | 复用原 Response，继续 Command Validation |
| `operation.started`，无 terminal Event | 查询 Ledger/外部系统；无法确认则进入 `UNKNOWN` |
| `tool.completed + operation.completed` | 本轮工具操作已收尾，可以进入下一轮 |

纠正过的误区：

- `model.completed` 的关键不是“模型调用结束”，而是完整 Response 已成为持久事实。
- `operation.started` 只证明执行意图已登记，不能证明外部效果成功或失败。
- 最大重试次数不能解决重复付款、重复部署等副作用；安全重试还需要幂等键和明确策略。
- Response 可以直接读取原值；External Effect 跨越 Harness 与外部系统两个持久域，通常需要对账。

面试记忆句：

> 已有持久结果就复用，外部效果不明就对账；`model.completed` 后不重复采样，`operation.started` 后不盲目重试。

## 下一步：Block 3

主题：Goal、Exit Criteria 与 Loop Engineering。

需要证明：

- LocalPlan 全部完成不等于 Assignment 合格。
- CompletionGate 必须同时检查输出 Schema、必要 Evidence 和 Pending Operations。
- 重复 Action 只有产生 Evidence Delta 才算进展。
- Nudge 必须有明确原因和独立预算，预算耗尽后不能无限循环。
