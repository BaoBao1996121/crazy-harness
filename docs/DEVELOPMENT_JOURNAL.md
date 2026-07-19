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

### 03:05 Repo Maintainer 获得同任务 Team 路径

- **动作**：新增 `RepoMaintainerTeamTaskPack`，让 Scout 检查源码与测试、Builder 发起一次受控 Peer 对账后修改和验证、Reviewer 独立复验；Runtime 按持久 `run.created.task_pack` 动态恢复 Team TaskPack。
- **证据**：三个 Spike 全部通过；Team 精确测试 `3 passed in 33.72s`，真实 Scripted Team Run 产生 `evidence.recorded -> artifact.recorded -> review.recorded -> run.succeeded`，最终工作区包含正确修复和一跳 A2A。
- **效果**：Single 与 Team 首次可以处理同一个字节固定 Repo Bug，不再拿 Repo 修复与 resident-demo 合成故事做伪对照。
- **边界**：Inspect 阶段暂以源码和测试原文诊断；当前 `ToolResult.status` 把“工具执行失败”和“测试断言失败”合在一起，失败测试还不能作为 CompletionGate 的成功 Evidence。该状态建模问题单列后续，不在本阶段放宽 Gate。

### 03:20 公平配对、机器评分与 Trace 报告贯通

- **动作**：新增 `PairedEvalContract`、Trace 聚合、推荐策略、Repo 独立机器 Scorer 和持久 `eval_id`；两臂必须同 Input Hash、同模型配置、同每臂总预算且工作区隔离，报告不读取 Agent 自述作为质量事实。
- **证据**：配对领域测试 `16 passed`；Scorer `2 passed`；Runtime 配对集成 `2 passed in 94.06s`。重开 Runtime 后报告完全相同；篡改 Team 测试文件后，即使原 Run 为 Succeeded，Scorer 仍判失败。回归同时发现并修复 Windows 同秒同长度改写可能复用旧 `.pyc` 的污染窗口。
- **效果**：平台现在能回答“两臂是否真的可比、实际产物是否正确、Team 多用了多少模型/工具/A2A”，并把确定性结果严格标成 `insufficient_live_evidence`，不会提前宣传 Team 更优。
- **边界**：本机无 DeepSeek Key，尚无多 Trial 真实采样、方差和置信区间；配对创建跨两个 Run 不是数据库原子事务，当前通过请求/失败事件和孤臂取消止损。此阶段未改前端，因此不新增截图。

### 03:57 公平评测进入 Control Room

- **动作**：新增“公平评测 / Eval”入口、配对创建对话框和全宽 Single/Team 对照带；前端从生成的 OpenAPI 契约读取持久报告，支持后台 Drain、轮询、URL/localStorage 恢复及两臂时间线切换。
- **证据**：API、Hook 与展示规则均经历 RED -> GREEN；前端全量 `13 files / 43 tests passed`，TypeScript + Vite production build 成功，1,599 modules transformed；1280px 与 390px 均无横向溢出，控制台 0 error、0 warning。
- **效果**：公平性证明、机器评分、模型/工具/A2A 开销、Token/费用和延迟首次与各自完整 Trace 放在同一界面；Scripted 证据始终显示“真实证据不足”。
- **边界**：前端只呈现后端持久事实，不自行计算可晋升结论；当前只有一个 Repo Golden Case，真实多 Trial 统计尚未接入。

### 04:04 v0.8 确定性 Golden Pair 实机贯通

- **动作**：在全新数据目录启动 `v0.8.0-dev`，通过正式 HTTP API 创建并 Drain 一组同题 Repo Pair，再从 Control Room 打开完成报告和两条 Run Timeline。
- **证据**：Eval `eval_28c7fe8192df`；Single `run_0a3bcc7903ac` 与 Team `run_473a344349bd` 均由独立 Scorer 得到 `100/100`。Single 为 7 次模型决策、6 次工具、0 次 A2A、约 13 秒；Team 为 19 次模型决策、9 次工具、1 次 A2A、约 33 秒。核心分层回归 `25 passed in 133.16s`，Store 热更新边界 `2 passed`，Ruff 全绿。桌面 1280x720 与移动 390x844 截图已在本次任务中直接呈现；移动 `scrollWidth <= innerWidth`，控制台 0 条告警或错误。
- **效果**：平台不只“能跑 Team”，而是能机械证明两臂是否可比、产物是否真实正确、Team 为协作多付出了什么；本次数据清楚展示了无质量增益时的协调开销。
- **边界**：模型为 Scripted Provider，Token/费用为 0，推荐结果严格保持 `insufficient_live_evidence`；这不是 Team 负收益的统计结论，也不能代替 DeepSeek 多 Trial。

### 05:05 Pair 提交恢复与 Scripted 游标续播

- **时间**：2026-07-19 05:05:30 +08:00。
- **动作**：将公平评测创建改为 Prepare -> Commit -> Release；Pair 的 Single Assignment 在提交前带释放门，提交后才进入持久邮箱；默认 Scripted 单 Agent 按已持久化的 `model.completed` 数量恢复响应游标。
- **证据**：响应丢失恢复与进程重启续播两个定向测试均通过，结果为 `2 passed in 14.69s`；恢复测试同时断言两次 `mailbox.delivery.sent` 均晚于唯一的 `eval.pair.committed`。
- **效果**：HTTP 响应丢失不会产生第二组付费 Run；Runtime 重启不会从第一条 Scripted Response 重放，也不会重复投递 Pair 的 Single 臂。
- **边界**：当前证据覆盖确定性模型与本地 SQLite 恢复；并发评分只执行一次、DeepSeek 完整模型证明和前端幂等键仍在下一小阶段验证。

### 05:11 并发评分唯一化与 DeepSeek 完整模型证明

- **时间**：2026-07-19 05:11:25 +08:00。
- **动作**：Finalizer 以 `eval-score:{eval_id}` 获取带 Fencing Token 的 SQLite Claim；DeepSeek Provider 暴露完整推理配置，并把它随 `model.call.reserved` 持久化，Eval Gate 对契约与每次物理调用逐项核对。
- **证据**：并发 Finalizer、Provider Profile、配置不一致拒绝三条定向测试结果为 `3 passed in 38.74s`；竞争测试证明整个 Pair 只执行两次 Scorer 调用，即每臂一次。
- **效果**：GET/list 保持纯读取，并发请求不会重复运行测试；只匹配 Provider 名与模型名已不再足够，端点、Thinking、采样约束、输出上限和超时也必须一致。
- **边界**：该测试用 Mock/Scripted 事实验证治理链，还没有声明本机完成付费 DeepSeek Pair；评分 Claim TTL 为初始工程值，后续需用更大评测集校准。

### 05:22 v0.8 加固版 Changed 回归通过

- **时间**：2026-07-19 05:22:15 +08:00。
- **动作**：对 Pair Service/Runtime/API、单 Agent、Team Model、Model Governance、Store、Scorer 和领域契约执行 Changed 回归；前端同步运行全量 Vitest、TypeScript 及 Vite Production Build。
- **证据**：后端 `109 passed in 562.57s`；前端 `14 files / 45 tests passed`；Vite 成功转换 1,600 modules；`python -m ruff check crazy_harness tests` 全绿。
- **效果**：Prepare/Commit、恢复、评分、模型证明与前端幂等键没有破坏既有 Agent Loop、Team Worker、API 和持久化边界，可以进入真实服务纵向验收。
- **边界**：这是 Windows 本地 Changed Stage，不替代 PR 上的 Linux/Windows Release CI；九分钟级回归只在阶段边界运行，普通探索继续使用秒级 Exact Stage。

### 05:33 加固版 Golden Pair 从真实 UI 贯通

- **时间**：2026-07-19 05:33:05 +08:00。
- **动作**：在全新数据目录启动 v0.8，通过 Control Room 表单创建 Pair；浏览器原生校验先发现费用默认值与 step 基准不兼容，改为允许任意正小数并重建，再由后台 Runtime 完成两臂、机器评分和报告持久化。
- **证据**：Eval `eval_5d3fd7637cf2`；Single `run_4d4c93dba07c` 与 Team `run_9e56c52a8e02` 均为 `100/100`。Single 为 7 模型、6 工具、0 A2A、约 14 秒；Team 为 19 模型、9 工具、1 A2A、约 34 秒。前端修复后仍为 `45 passed` 且 Production Build 成功；1280x720 的 `scrollWidth == innerWidth == 1280`，浏览器控制台 0 日志，实际截图已在任务会话中直接呈现。
- **效果**：前端请求幂等键、URL 状态、后端 Prepare/Commit、持久 Mailbox、两条 AgentLoop、独立 Scorer、Trace 聚合和 Control Room 展示首次在加固版本中共同跑通；真实浏览器验收还关闭了一个纯函数测试无法发现的表单阻断。
- **边界**：模型仍为 Scripted，Token/费用为 0，结论只能证明机制与开销可观测；没有 `DEEPSEEK_API_KEY` 时不能宣称 Team 质量收益，推荐继续严格为 `insufficient_live_evidence`。

设计审查：5/5 通过。没有新增外部 Runtime 依赖；延迟与测试数量均标为本机实测，未外推性能承诺；响应丢失、重复请求、并发评分、Fixture/Baseline/测试投毒、模型配置漂移与表单阻断均有失败路径；Claim TTL 与推荐阈值仍标为初始值待调优；范围仍限定在 Repo Maintainer 单 Pair，不冒充多 Trial 或任意任务收益。

### 06:06 Pair 请求重试语义分流

- **时间**：2026-07-19 06:06:30 +08:00。
- **动作**：把 Pair 创建错误分为“结果不确定、继续复用原 `request_id`”和“提交前已确定终止、返回 409 并换新 ID”；先写后端 HTTP/Service 与前端请求身份 RED，再补最小实现。
- **证据**：RED 分别得到 Python Import/收集失败和前端 ID 仍为 `0001` 的断言失败；GREEN 后后端 `2 passed`、前端 `2 passed`。
- **效果**：响应丢失仍能找回同一 Pair；预算/契约等提交前确定失败后，用户下一次点击不会被已经封存的幂等键永久卡住。
- **边界**：409 只证明本地 Pair 创建已终止；任何已经触达外部 Provider 或工具的副作用仍需 Ledger、业务幂等键或对账，不能据此宣称撤销。

### 09:04 v0.8 合并前终审关闭五条公平性盲区

- **时间**：2026-07-19 09:04:30 +08:00。
- **动作**：只读终审先复现 4 个 P1 与 1 个 P2，随即暂停发布；分别加入 Fixture 原子发布与双 Hash 校验、Eval Create Claim + fencing 终态、Live 无效证据报告、浏览器待确认 Request/Draft 恢复，以及 Scripted 角色脚本清单 Hash 和诚实 UI 标签。
- **证据**：两条 Fixture RED 转绿为 `2 passed`；并发 Create RED 原先稳定产生 `failed + committed`，加固后 `1 passed` 且只保留唯一 `failed`；零模型调用 Live Pair RED 原先抛异常并停在 `running`，加固后与 Profile 漂移测试为 `2 passed`；Scripted Profile 清单测试为 `1 passed in 37.88s`；浏览器刷新请求恢复与脚本标签由前端 RED/GREEN 锁定，全量当时为 `48 passed`。
- **效果**：`input_hash` 终于绑定真实发布的初始字节；并发或过期执行者不能补写矛盾终态；失败样本与无效证据得到区分；响应丢失跨刷新仍找回原 Pair；Scripted 不再冒充“同模型”。
- **边界**：Create Claim TTL 是本地准备阶段的初始值且没有跨数据库事务；浏览器恢复会暂存任务 Draft，部署到共享终端前应迁移到受保护的服务端 Request Registry；真实 DeepSeek 多 Trial 仍未执行。

### 09:18 v0.8 终审版发布验收

- **时间**：2026-07-19 09:18:37 +08:00。
- **动作**：在五条终审修复后重跑 Pair Service/Runtime/API、Scorer、Single Agent 与领域契约回归；重新执行全前端测试、TypeScript/Vite 生产构建、Ruff 与 diff check，并用最新版进程恢复 Golden Pair 做真实浏览器验收。
- **证据**：后端发布聚焦组 `53 passed in 321.46s`；前端 `14 files / 50 tests passed`；生产构建成功，1,600 modules transformed；Ruff 与 `git diff --check` 全绿。健康接口返回 `v0.8.0-dev`；浏览器恢复 Eval `eval_5d3fd7637cf2`，明确展示“不同确定性脚本 / Different deterministic scripts”，1280x720 下 `scrollWidth == clientWidth == 1280`，控制台 0 日志；创建对话框也按 Scripted/Live 动态展示公平性语义，实际桌面截图已在本次任务中直接呈现。
- **效果**：发布候选同时证明可恢复 Pair 创建、唯一终态、原子 Fixture、失败样本报告、跨刷新请求身份和诚实 Scripted 展示；生产构建额外发现并关闭了单测夹具漏加 `evidence_valid / invalid_reasons` 的类型缺口。
- **边界**：本轮没有独立改变响应式布局，因此沿用同一前端结构此前的 390px 验收，只重新实测 1280px；本机仍无 `DEEPSEEK_API_KEY`，没有 Live 多 Trial、统计置信度或真实费用结论。

设计审查：5/5 通过。没有新增外部 Runtime 依赖；全部测试、构建、视口与 Golden Pair 数字均标为本机实测；并发创建、部分 Fixture、零调用 Live 失败、响应丢失和无效证据路径均有覆盖；Claim TTL、最小 Trial 数与推荐阈值仍标为初始值；范围限定在 v0.8 Repo Maintainer 公平配对发布候选。
