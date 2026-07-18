# 阶段突破日志

> 目的：用很短的记录保留 Crazy Harness 每个小阶段“何时做了什么、证据是什么、产生了什么效果、边界在哪里”。

## 记录规则

- 只有出现可复现的新能力、风险关闭或关键认知变化时才记，不把普通过程写成突破。
- 每条固定包含：时间、动作、证据、效果、边界。
- 涉及前端时，至少附一张可辨认变化、能实际解码打开的截图，并注明 Run ID 与视口；通常同时验收桌面和 390px 移动端，不能只检查文件是否存在。
- 纯后端阶段附最窄复现命令或持久事件，不为了截图额外制造 UI。
- 本文件记录事实；规划与待办仍以 `GENERAL_AGENT_TEAM_MASTER_PLAN.md` 和 `PROJECT_PROGRESS.md` 为准。

## 2026-07-18

### 22:43 建立阶段突破记录协议

- **动作**：把“每个小阶段完成后立即留下简短记录”纳入长期开发规则。
- **证据**：本文件及后续每个纵向切片的追加记录。
- **效果**：可以按时间回放平台能力怎样形成，并直接用于学习复盘、项目讲解和发布说明。
- **边界**：日志不替代测试、Trace、ADR 或完整技术文档，只提供高信号索引。

### 22:47 v0.7 三个关键假设通过

- **动作**：验证 Team 模型模式重建、SQLite 并发预算预约、DeepSeek 非 thinking 工具调用三条设计前提。
- **证据**：`verify_team_model_mode_rebuild.py`、`verify_sqlite_atomic_model_budget.py`、`verify_deepseek_non_thinking_tool_call.py` 全部输出 `PASS`；三个脚本分别为 13、18、17 行。
- **效果**：确认可以用根 Run 选择子 Agent 模型，用 SQLite 短事务阻止并发超支，并在当前消息协议不支持 `reasoning_content` 时先安全关闭 thinking。
- **边界**：DeepSeek 证据来自官方接口契约与 `MockTransport`，本机未配置 API Key，不能宣称在线调用已经成功。

### 23:02 Team 子 Agent 在线模型路由贯通

- **动作**：允许 Team Run 选择 `deepseek`；为 Assignment/Peer AgentRun 建立持久身份绑定并注入独立 Provider；把根 Run 的模式贯穿到 RuntimeManifest、CapabilityCompiler、ToolSpec 与 ToolPolicy。
- **证据**：两个新增 RED 先分别因 Provider 参数和 Team Factory 缺失失败；修复后精确用例 `2 passed`，Provider/Team 邻接组 `5 passed`，受影响文件 Ruff 全绿。
- **效果**：Supervisor 继续确定性编排，但四个 Assignment 和一次受控 Peer 对账都可以真实走在线模型；模型看见的模式与 Harness 实际授权一致。
- **边界**：当前测试用注入的确定性 Provider 代替付费 API；本机没有 DeepSeek Key，且持久预算、重试、成本核算尚未接入。本阶段未改前端，因此不新增截图。

## 2026-07-19

### 00:18 模型调用成为持久受治理操作

- **动作**：新增 SQLite `model_calls` 账本与 `PersistentModelCallAuthority`，在 HTTP 前原子预约 Token、估算费用和并发槽；记录物理 Attempt，并在持久 `model.completed` 后幂等核销 usage。
- **证据**：并发竞争、预算拒绝、401、429/5xx 重试、ReadTimeout Unknown、响应复用和重复核销均有确定性测试；模型治理前端纯函数 RED 后 GREEN。
- **效果**：模型调用现在可回答“能否发生、发生几次、花了多少、崩溃后怎么算”，而不只是一次不可观察的 `provider.complete()`。
- **边界**：输入预约使用 UTF-8 字节近似，费用按 2026-07-18 DeepSeek 官方价卡估算；本机无 Key，未宣称得到真实账单或付费在线成功。

### 00:36 关闭跨层重试放大

- **动作**：把 Provider、Scheduler、Supervisor 三层重试职责拆开；终态模型失败提交 `run.failure.requested` 并失败整个 Run，普通 Stage 重派增加 `max_stage_attempts`，重启可补齐失败终态。
- **证据**：原回归能把一次 401 持续重派到 100 个 Scheduler step；收紧后用例在 8 次投递内要求 Run Failed、Mailbox 清空且物理传输恰好 1 次，最终精确组 `2 passed`、模型与 Supervisor 邻接组 `10 passed`。
- **效果**：401 不再从“Provider 不重试”绕到 Supervisor 无限重派；默认三层上限不再把一次业务动作理论放大成 `3 x 3 x 3 = 27` 次调用。
- **边界**：并发额度不足仍作为普通 Assignment 失败，当前最多重派 3 次后 Blocked；后续应把它细分为等待型背压，而不是立即失败。

### 00:58 模型治理进入可观察 Control Room

- **动作**：新增“模型 / Model Governance”视图，展示运行级 Token/费用额度、Completed/In-flight/Unknown 汇总，以及按 AgentRun 隔离的模型调用账本；Team 新建对话框在配置 Key 后允许选择 DeepSeek。
- **证据**：最终确定性完整演示 `run_6d81aa6b2481` 成功结束，18 个 Scheduler step、11 个已完成模型调用、1,408 Token、估算 198 microUSD，Active/Unknown 均为 0；浏览器实测桌面 1280x720 与移动 390x844，控制台 0 条日志。截图已在本次任务会话中直接呈现并实际解码，不依赖被本机 E-SafeNet 改写的磁盘图片。
- **效果**：学习者可以把“模型调用”从 Timeline 中的一行事件展开为预算、状态机、物理 Attempt 和成本，直接观察不同 Agent/Peer 的调用隔离。
- **边界**：这组数字来自确定性 Provider 的合成 usage，只证明完整 Harness 合同与前端工作，不证明 DeepSeek 线上质量、延迟或真实账单；费用始终标记 Estimate。

### 01:00 v0.7 本地分层回归通过

- **动作**：按 Changed/Smoke/Core/Release 节奏，只在本地运行受影响 Exact、Smoke、Ruff、前端测试和生产构建，把全量跨平台 Release 留给 PR CI。
- **证据**：Exact `46 passed in 140.67s`；Smoke `7 passed, 269 deselected in 29.68s`；Ruff 全绿；前端 `10 files / 33 tests passed`；Vite 构建 1,594 modules 成功。
- **效果**：关键模型治理、失败恢复、AgentLoop、Supervisor、API 和单 Agent 邻接边界都得到快速反馈，同时避免每个探索切片都阻塞在九分钟级全量回归。
- **边界**：本地结果不是 Release 结论；Linux/Windows 全量确定性回归必须以本 PR 的 GitHub Actions 为准。

设计审查：5/5 通过。外部 DeepSeek API、错误分类与价卡均来自 2026-07-18 官方文档；实测与 Estimate 已区分；超时、401、预算不足、Unknown、崩溃恢复和跨层重试路径均有覆盖；重试/并发/预算阈值均标注为初始值；未越界宣称付费在线成功、Remote A2A 或 Single-vs-Team 收益。

### 01:20 关闭响应落盘前崩溃与脏 usage 窗口

- **动作**：AgentLoop 开始识别“已有 `model.requested`、没有 `model.completed`”的半成品 Turn；无物理 Attempt 的预约可幂等释放并新建 Turn，已有 Attempt 则转 Unknown 并禁止重采样。Provider usage 缺失、负数或类型错误时改按原预约悲观核销。
- **证据**：旧实现先被 RED 证明会再次调用 Provider；修复后“in-flight 不重采样”和“零 Attempt 可跨二次崩溃安全重试”均通过。脏 usage RED 原先抛 `ValueError`，修复后按预约 Input/Output/Cost 核销并写 `usage_quality=pessimistic_fallback`；最终 Exact `49 passed in 179.31s`、Smoke `7 passed, 273 deselected in 30.74s`，Ruff 与 diff check 全绿。
- **效果**：最危险的“Provider 已收费但 Harness 没记下 Response”不再被当成普通失败重试；外部 usage 也不能通过脏值降低预算占用。
- **边界**：Unknown 仍不能自动得到原响应，只能阻止重复付费并等待对账；当前选择可控失败而非猜测恢复。Team 付费 Smoke 已提供显式 opt-in 测试，但本机无 Key，当前结果为 1 skipped。

### 01:51 失败 Run 成为持久写屏障

- **动作**：把终态模型失败改成两阶段协议：Worker 只提交按失败请求身份唯一化的 `run.failure.requested`；Runtime 在 Delivery 提交后封禁本地调度、失效远端 Claim bundle、补齐 Assignment/Lease/Mailbox，最后写 `run.failed`。Usage 账本已完成但审计 Event 丢失时，重放会补写 `model.usage.recorded`。
- **证据**：四个新用例先同时 RED，分别复现迟到 Worker 写入、远端已 Claim 邮箱残留、并发失败 Event ID 冲突和 Usage 审计缺口；实现后 `4 passed`。邻接回归额外捕获“错误封禁 succeeded 会阻断 Dream”，收窄为仅 Failed/Cancelled 执行封禁后，在线 Team 确定性路由与终态失败用例 `2 passed`。
- **效果**：跨进程迟到 Worker 不能在失败 Run 上继续制造执行事实；崩溃 Runtime 持有的消息不会永久留在邮箱；两个 Agent 同时遇到终态模型错误也不会互相撞坏 Event 幂等键；账本金额与审计 Trace 可分别恢复。
- **边界**：并发模型槽不足仍会消耗有界 Stage Attempt；已持久化 retry 计划但在退避期间崩溃，当前仍悲观进入 Unknown。这两项保留给等待型背压与 Retry Resume 纵切，不在 v0.7 发布前扩张状态机。

设计审查：5/5 通过。没有新增外部依赖或性能承诺；迟到写入、Claim 残留、并发冲突、审计丢失与成功后 Dream 误伤均有测试；Claim TTL 与重试阈值仍明确为初始值；范围限定在 v0.7 发布加固，等待型背压和 Retry Resume 未冒充完成。

### 02:02 v0.7 发布候选重新验收

- **动作**：在最终加固代码上重跑分层回归，重启 8768 服务，并重新打开 Golden Run 的模型治理页做桌面实机验收。
- **证据**：可靠性受影响组 `59 passed in 74.14s`；未重复的 AgentLoop/API/单 Agent 邻接组 `35 passed in 123.98s`；Smoke `7 passed, 277 deselected in 26.91s`；全项目 Ruff 全绿。课程检查 17/17 Required Checks 通过，状态 `ready_with_external_gates`。健康接口返回 `v0.7.0-dev`；`run_6d81aa6b2481` 为 Succeeded，11 Completed、0 In-flight、0 Unknown。最终桌面截图为 1280x720，页面宽度与视口同为 1280；同一前端构建此前移动端 390x844、控制台 0 日志验收仍有效。
- **效果**：发布候选同时具备可运行 Team 模型治理、失败恢复防线、双语可观察界面和可复现学习证据，可以进入 PR 的跨平台 Release CI。
- **边界**：本地仍未执行付费 DeepSeek Team 调用；前端没有在本轮后端加固后重新构建，因为加固后没有再修改前端源码，沿用此前 `33 passed` 与 production build 结果。
