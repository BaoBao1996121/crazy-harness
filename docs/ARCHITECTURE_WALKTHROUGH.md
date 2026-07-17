# Crazy Harness 架构走读

> 日期：2026-07-10
> 状态：目标架构 v0.2 已确认；MVP-0 代码仍是薄脚手架，核心机制尚未做实

## 一句话结论

当前代码已经有 `Full Topology Thin Harness` 的第一版形状，但目标架构在本轮拷问后明显更完整。阅读时必须区分“已有代码”和“已确认但待实现”，不能把文档里的目标语义当成当前能力。

## 当前脚手架

结论：现有代码提供入口、最小事实存储和四个手写槽位，尚不具备真正的常驻恢复、受控 A2A 和 Loop Engineering。

```mermaid
graph TD
  CLI["命令行接口（CLI）：crazy run / replay"] --> Runtime["运行时（Runtime）"]
  Runtime --> EventLog["事件日志（EventLog）：events.jsonl"]
  Runtime --> ArtifactStore["制品存储（ArtifactStore）：artifacts/"]
  Runtime --> World["CI/CD 业务适配器（CI/CD World Adapter）"]

  World --> Team["发布团队角色卡（Release Team Cards）"]
  World --> WorldArtifacts["风险报告 / 发布计划 / 审查决定 / 运行报告（World Artifacts）"]
  World --> Tools["CI/CD 工具（CI/CD Tools）"]

  Tools --> ToolRegistry["工具注册表（ToolRegistry）"]
  ToolRegistry --> LocalTools["本地工具（Local Tools）：git.status / git.diff / test.run"]
  ToolRegistry --> DryTools["演练工具（Dry-run Tools）：build.mock_plan / volcengine.plan"]

  Runtime --> AgentLoop["Agent 循环待手写（AgentLoop TODO）"]
  AgentLoop --> Context["每轮微压缩待手写（Microcompact TODO）"]
  AgentLoop --> PromptPack["提示词包待手写（PromptPack TODO）"]
  AgentLoop --> ModelProvider["模型提供器（ModelProvider）：DeepSeek / FakeModelProvider"]
  AgentLoop --> A2A["Agent 通信总线待手写（A2ABus TODO）"]
  AgentLoop --> ToolRegistry
  AgentLoop --> EventLog
  AgentLoop --> ArtifactStore

  PromptPack --> RuntimeManifest["运行时清单（RuntimeManifest）"]
  A2A --> Agents["总控 / 侦察 / 构建 / 审查 Agent（Coordinator / Scout / Builder / Reviewer）"]
```

| 图中名词 | 简短含义 |
|---|---|
| 命令行接口（CLI） | 用户启动运行、重放或审批操作的入口。 |
| 运行时（Runtime） | 组装一次 run 所需组件，并驱动当前薄版执行流程。 |
| 事件日志（EventLog） | 将发生过的事件追加写入 `events.jsonl`。 |
| 制品存储（ArtifactStore） | 保存大日志、报告和其他可引用结果的文件目录。 |
| CI/CD 业务适配器（World Adapter） | 把代码变更等业务事件翻译成 Core 能理解的 Event 和 Tool。 |
| 发布团队角色卡（Release Team Cards） | 声明 Coordinator、Scout、Builder、Reviewer 的职责与能力。 |
| World Artifacts | CI/CD World 定义的 RiskReport、ReleasePlan 等结构化产物。 |
| CI/CD 工具（CI/CD Tools） | 业务侧提供的 git、测试、构建计划和火山云规划工具。 |
| 工具注册表（ToolRegistry） | 保存工具 schema、策略元数据与具体执行函数。 |
| 本地工具（Local Tools） | 直接读取仓库状态、diff 或运行测试的真实工具。 |
| 演练工具（Dry-run Tools） | 只生成构建或云操作计划，不产生危险真实副作用。 |
| Agent 循环（AgentLoop） | 把 Context、模型候选动作、工具执行和事件记录串起来的核心控制流。 |
| 每轮微压缩（Microcompact） | 每次构造 Context 时执行的无 LLM 清理与引用化。 |
| 提示词包（PromptPack） | 将角色、任务、运行时信息和策略编译成模型 messages。 |
| 模型提供器（ModelProvider） | 屏蔽 FakeModel 与 DeepSeek 调用格式差异的端口。 |
| Agent 通信总线（A2ABus） | 通过消息而非直接方法调用连接多个 AgentInstance。 |
| 运行时清单（RuntimeManifest） | 告诉模型当前模式、时间、工作目录和可用能力等动态事实。 |
| 四类 Agent | Coordinator 负责全局计划；Scout 收集信息；Builder 执行；Reviewer 独立验收。 |

## 目标静态架构

结论：目标系统把持久事实、可重建状态、单轮上下文和外部副作用分成四个层次。

```mermaid
graph TD
  World["CI/CD 业务适配器（World Adapter）"] --> Ingress["事件入口（Event Ingress）"]
  CLI["命令行与人工审批（CLI / Human Approval）"] --> Ingress
  Ingress --> EventLog["追加式事实日志（EventLog）"]

  EventLog --> Reducers["归约器与状态投影（Reducers / Projections）"]
  Reducers --> TaskState["任务 / 委派 / Agent 状态（Task / Assignment / Agent Status）"]
  Reducers --> Mailbox["持久邮箱与等待条件（Mailboxes / Wait Conditions）"]
  Reducers --> LocalPlan["最新局部计划（Latest LocalPlan）"]
  Reducers --> OpLedger["操作账本（Operation Ledger）"]

  Scheduler["协作式调度器（Cooperative Scheduler）"] --> Mailbox
  Mailbox --> AgentInstance["常驻 Agent 实例（Resident AgentInstance）"]
  AgentInstance --> AgentLoop["可恢复 Agent 循环（Recoverable Agent Loop）"]

  AgentLoop --> ContextBuilder["上下文构造与每轮微压缩（ContextBuilder + Microcompact）"]
  ContextBuilder --> PromptContract["提示词运行时契约（Prompt Runtime Contract）"]
  ContextBuilder --> ArtifactStore["制品存储与历史服务（ArtifactStore / History Service）"]
  ContextBuilder --> ContextManifest["上下文清单，仅供审计（ContextManifest）"]
  PromptContract --> ModelPort["模型端口（ModelPort）"]
  ModelPort --> Scripted["脚本化测试模型（ScriptedModel）"]
  ModelPort --> DeepSeek["真实模型（DeepSeek V4 Flash）"]

  ModelPort --> Validator["决策校验器（Decision Validator）"]
  Validator --> Hooks["钩子（Hooks）"]
  Hooks --> Policy["硬策略与审批（Hard Policy / Approval）"]
  Policy --> Dispatcher["动作分发器（Action Dispatcher）"]

  Dispatcher --> ToolScheduler["工具调度器与顺序屏障（Tool Scheduler / Barriers）"]
  Dispatcher --> A2A["Agent 间通信总线（A2A Bus）"]
  Dispatcher --> PlanPatch["计划增量修改（Plan Patch）"]
  Dispatcher --> Completion["等待 / 提交 / 阻塞（Wait / Submit / Blocked）"]

  ToolScheduler --> Runtime["受限本地 / Docker / 浏览器运行时（Execution Runtime）"]
  ToolScheduler --> OpLedger
  A2A --> EventLog
  Runtime --> EventLog
  Completion --> EventLog
  PlanPatch --> EventLog
  EventLog --> Trace["轨迹 / 恢复 / 重放 / 评估（Trace / Recovery / Replay / Eval）"]
```

| 图中名词 | 简短含义 |
|---|---|
| 业务适配器（World Adapter） | 把特定业务平台的事件和操作映射到通用 Core。 |
| 事件入口（Event Ingress） | 接收 World、CLI、人工审批或定时器产生的新事件。 |
| 人工审批（Human Approval） | 对高风险动作或关键决策进行人工确认。 |
| 追加式事实日志（EventLog） | 只追加不就地修改的事实源，保存系统真实历史。 |
| 归约器（Reducer） | 按顺序处理事件，把历史折叠成当前状态。 |
| 状态投影（Projection） | 可从 EventLog 重建的 Task、Mailbox、Plan 等当前视图。 |
| 任务（Task） | 一个根事件引起的整组协作工作。 |
| 委派（Assignment） | Coordinator 分给某个 AgentInstance 的单份工作。 |
| Agent 状态（Agent Status） | 描述实例当前是空闲、忙碌、等待、降级还是离线。 |
| 持久邮箱（Mailbox） | 保存某 Agent 尚未消费的事件或消息。 |
| 等待条件（WaitCondition） | 描述 Agent 等待哪类事件、关联 ID、来源和截止时间。 |
| 局部计划（LocalPlan） | 执行 Agent 在当前 Contract 内维护的结构化步骤。 |
| 操作账本（Operation Ledger） | 记录可能产生外部副作用的动作阶段、幂等键和对账状态。 |
| 协作式调度器（Cooperative Scheduler） | 在单进程中确定性地选择下一个可运行 Agent。 |
| 常驻 Agent 实例（Resident AgentInstance） | 拥有持久身份和邮箱、可跨事件继续工作的逻辑实体。 |
| 可恢复 Agent 循环（Recoverable Agent Loop） | 记录 phase 转移并能从崩溃点继续的单 Agent 内核。 |
| 上下文构造器（ContextBuilder） | 每轮选择保护槽位、历史、消息和 Artifact 组成 Context。 |
| 每轮微压缩（Microcompact） | 规则驱动地清理旧表示和引用化大内容，不调用 LLM。 |
| 提示词运行时契约（Prompt Runtime Contract） | 将角色、任务、策略和运行事实编译成稳定提示结构。 |
| 制品存储（ArtifactStore） | 保存大输出、报告、截图和其他可回溯证据。 |
| 历史服务（History Service） | 在权限检查后按 ref 或关键词读取历史事实。 |
| 上下文清单（ContextManifest） | 记录本轮包含和排除了什么、如何变换及原因，只供审计。 |
| 模型端口（ModelPort） | 为真实模型与测试模型提供统一调用接口。 |
| 脚本化测试模型（ScriptedModel） | 按预设响应运行，用于确定性测试、故障注入和恢复验证。 |
| 决策校验器（Decision Validator） | 把模型输出规范化并检查 schema 与 contract version。 |
| 钩子（Hook） | 在关键阶段观察、拒绝、请求审批或提出可审计修改。 |
| 硬策略（Hard Policy） | 模型、Skill 和 Hook 都不能绕过的权限与安全边界。 |
| 动作分发器（Action Dispatcher） | 根据 command 类型把动作交给工具、A2A、计划或完成处理器。 |
| 工具调度器与屏障（Tool Scheduler / Barriers） | 并发安全调用，遇到写操作等不安全调用时保持顺序。 |
| A2A 通信总线（A2A Bus） | 持久投递 Agent 间消息，并处理 ack、重试和去重。 |
| 计划增量修改（Plan Patch） | 带版本地新增、完成、取消或修订 LocalPlan 步骤。 |
| 等待 / 提交 / 阻塞 | Agent Loop 的三类非工具控制动作。 |
| 执行运行时（Execution Runtime） | 在本地、容器或浏览器环境中真正执行工具。 |
| 轨迹 / 恢复 / 重放 / 评估 | 分别负责过程观测、继续任务、复现历史和衡量质量。 |

### 四层含义

| 层 | 代表组件 | 回答的问题 |
|---|---|---|
| 持久事实 | EventLog、ArtifactStore、OperationLedger | 真实发生过什么，外部动作是否可能已生效 |
| 可重建投影 | TaskState、AssignmentState、Mailbox、LocalPlan | 进程重启后现在处于什么阶段 |
| 单轮认知输入 | ContextBuilder、PromptContract、ContextManifest | 这一轮模型被允许看到什么，为什么 |
| 受控执行 | Validator、Hook、Policy、ToolScheduler、Runtime | 模型建议的动作是否能执行，如何避免越权和重复副作用 |

## 单 Agent 一轮

结论：每一轮都从持久事实重新编译上下文，模型只产出候选 command，Harness 完成校验、执行和状态转移。

```mermaid
sequenceDiagram
  participant S as 调度器（Scheduler）
  participant L as Agent 循环（Agent Loop）
  participant E as 事件与状态存储（Event Store）
  participant C as 上下文构造器（Context Builder）
  participant M as 模型端口（Model Port）
  participant G as 校验与授权门（Validation Gate）
  participant X as 动作执行器（Action Executor）
  participant A as 制品存储（Artifact Store）
  S->>L: 投递邮箱事件（deliver mailbox event）
  L->>E: 加载委派、计划和操作状态（load state）
  L->>C: 构造保护槽位与选中历史（build context）
  C->>A: 解析授权引用或卸载大内容（resolve refs / offload）
  C-->>L: 返回模型消息与上下文清单（messages / ContextManifest）
  L->>E: 记录模型调用开始（model.call.started）
  L->>M: 请求完整响应或流式响应（complete / stream）
  M-->>L: 返回模型候选响应（provider response）
  L->>E: 保存原始引用与规范化候选（persist candidate）
  L->>G: 校验结构、契约版本、策略和预算（validate）
  alt 无效或被拒绝（invalid or denied）
    G-->>L: 返回结构化错误或拒绝（error / denial）
    L->>E: 记录校验失败（validation.failed）
  else 已授权（authorized）
    G-->>L: 返回已授权命令（authorized command）
    L->>X: 执行原子动作或安全工具批次（execute action / batch）
    X-->>L: 返回成功、未知或错误（result / unknown / error）
    L->>A: 卸载大结果（offload large result）
    L->>E: 记录操作、结果与状态事件（record events）
  end
  L-->>S: 继续、等待、已提交或失败（continue / wait / submitted / failed）
```

| 图中名词 | 简短含义 |
|---|---|
| 调度器（Scheduler） | 从可运行实例中选择本轮由谁执行。 |
| Agent 循环（Agent Loop） | 驱动一次 Context、模型、校验、动作和记录的状态机。 |
| 事件与状态存储（Event Store） | EventLog 保存事实，Reducer 从事实重建当前状态。 |
| 上下文构造器（Context Builder） | 选择保护槽位、近期历史、邮箱和 Artifact 组成模型输入。 |
| 模型端口（Model Port） | 统一 ScriptedModel 与 DeepSeek 的调用方式。 |
| 校验与授权门（Validation Gate） | 组合 schema、contract version、Hook、Policy、审批和预算检查。 |
| 动作执行器（Action Executor） | 执行工具、A2A 消息、Plan Patch 或提交等原子 command。 |
| 制品存储（Artifact Store） | 保存大结果并返回可放入 Context 的引用。 |
| 保护槽位（Protected Slots） | Policy、AgentCard、AssignmentContract 等不可被压缩替代的 latest-only 内容。 |
| 上下文清单（ContextManifest） | 审计这一轮模型看到了什么、没看到什么以及原因。 |
| Provider 响应（Provider Response） | 模型供应商返回的原始内容，先落盘再规范化。 |
| 规范化候选（Normalized Candidate） | Provider Adapter 转换出的内部 typed command 候选。 |
| 契约版本（Contract Version） | 标识动作依据的 AssignmentContract 版本，防止按旧任务执行。 |
| 已授权命令（Authorized Command） | 通过全部校验后才允许交给执行器的动作。 |
| 安全工具批次（Safe Tool Batch） | 相邻且显式并发安全、可以同时执行的一组完整工具调用。 |
| 结果未知（UNKNOWN） | 外部动作可能成功但本地没有确定结果，需要先对账。 |

关键崩溃边界：

- 模型响应已记录、动作未开始：恢复时复用响应，不再次调用模型。
- 操作已标记 started、外部结果未知：恢复为 `UNKNOWN`，先对账。
- 结果已记录、状态投影未更新：reducer 从 EventLog 重建，不重复产生业务效果。

## Teamwork 运行关系

结论：Coordinator 动态维护全局计划，普通 Agent 只在当前 Assignment 内做有界一跳协作。

```mermaid
sequenceDiagram
  participant W as 业务环境（World）
  participant C as 总控 Agent（Coordinator）
  participant S as 侦察 Agent（Scout）
  participant B as 构建 Agent（Builder）
  participant R as 审查 Agent（Reviewer）
  W->>C: 代码变更事件（code.changed event）
  C->>C: 检查角色卡与当前任务状态（AgentCards / TaskState）
  par 并行独立委派（independent assignments）
    C->>S: 委派契约：评估风险（AssignmentContract）
    C->>B: 委派契约：运行确定性检查（AssignmentContract）
  end
  S-->>C: 风险报告与证据引用（RiskReport / evidence refs）
  B->>S: 契约内请求补充证据（evidence.request）
  S-->>B: 返回证据与引用（evidence.response）
  B->>R: 请求审查并提交证据包（review.request / EvidencePack）
  R-->>B: 按准出项请求修订（revision.request）
  B->>R: 提交修订制品与新证据（revised artifact）
  R-->>C: 返回逐项审查结论（criterion verdicts）
  C->>C: 根据当前事实重新规划（replan）
  C-->>W: 运行报告、等待审批或继续委派（RunReport / next action）
```

| 图中名词 | 简短含义 |
|---|---|
| 业务环境（World） | 产生业务事件并接收最终动作或报告的外部环境。 |
| 总控 Agent（Coordinator） | 维护全局计划、创建 Assignment，并根据结果决定下一步。 |
| 侦察 Agent（Scout） | 读取 diff、配置和历史证据，识别风险与缺失信息。 |
| 构建 Agent（Builder） | 运行确定性检查、构建计划或生成候选发布产物。 |
| 审查 Agent（Reviewer） | 独立按 Exit Criteria 检查 Builder 的交付。 |
| 代码变更事件（code.changed） | CI/CD World 中触发本次 Task 的根事件。 |
| 角色卡（AgentCard） | 声明角色能力、接受的输入和可产出的 Artifact。 |
| 任务状态（TaskState） | 由历史事件归约出的全局任务当前状态。 |
| 并行独立委派（Independent Assignments） | 没有数据依赖的 Assignment 可以交给不同 AgentInstance 同时执行。 |
| 委派契约（AssignmentContract） | 每次委派携带的目标、准出条件、权限、预算和证据要求。 |
| 风险报告（RiskReport） | Scout 提交的结构化风险、缺失上下文和建议检查。 |
| 证据引用（Evidence Ref） | 指向 Artifact 或 Event 的受控引用，不直接复制完整上下文。 |
| 证据请求 / 响应 | 普通 Agent 在当前 Contract 内允许的一跳 A2A 对账。 |
| 证据包（EvidencePack） | Reviewer 被授权看到的目标、标准、候选产物、证据和已知限制。 |
| 修订请求（Revision Request） | Reviewer 对未满足 criterion 给出的结构化可修复意见。 |
| 逐项审查结论（Criterion Verdicts） | 对每条 Exit Criterion 分别给出通过、失败和证据。 |
| 重新规划（Replan） | Coordinator 根据最新事实更新滚动 GlobalPlan。 |
| 运行报告（RunReport） | Task 当前结果、时间线、产物、经验和下一步的汇总。 |

这个图不是固定业务链。Scout、Builder、Reviewer 是否出现、是否并行、是否需要第二轮修订，都由当前事件、AgentCard、证据和 Contract 决定。

## 已实现模块

### 入口层

- `crazy_harness/cli.py`
  - 支持 `crazy run dev-release --mode ...`
  - 支持 `crazy replay events.jsonl`
  - `run` 进入 CI/CD world，`replay` 读取事件日志。

- `crazy_harness/core/runtime/runner.py`
  - 创建 `run_id`
  - 创建 `runs/<run_id>/events.jsonl`
  - 创建 `runs/<run_id>/artifacts/`
  - seed 一个 `world.event.code_changed`
  - 当前 `run()` 后续故意 `NotImplementedError`，等待核心 loop 接上。

### 核心基础设施

- `crazy_harness/core/events/log.py`
  - append-only JSONL event log。
  - 支持 `append()` 与 `read_all()`。

- `crazy_harness/core/artifacts/store.py`
  - 文件型 artifact store。
  - 支持 `write_json()`、`write_text()`、`read_text()`。

- `crazy_harness/core/models/providers.py`
  - `FakeModelProvider`：测试用确定性模型。
  - `DeepSeekOpenAIProvider`：OpenAI-compatible DeepSeek chat completions provider。

- `crazy_harness/core/tools/registry.py`
  - 工具注册与调用。
  - `ToolSpec` 已包含 `use_when`、`do_not_use_when`、`side_effect_level`、`approval_required`、`output_offload_policy` 等 ToolPolicy 字段。

- `crazy_harness/core/hooks/manager.py`
  - 薄版 hook manager。

- `crazy_harness/core/policy/stop.py`
  - 薄版 stop policy。

### CI/CD World

- `crazy_harness/worlds/cicd/agents.py`
  - 定义 4 个 agent card：
    - Coordinator
    - Scout
    - Builder
    - Reviewer

- `crazy_harness/worlds/cicd/artifacts.py`
  - 定义 CI/CD world artifact schema：
    - `RiskReport`
    - `ReleasePlan`
    - `ToolExecutionResult`
    - `ReviewDecision`
    - `RunReport`
    - `ProgressReport`
    - `UserNotice`

- `crazy_harness/worlds/cicd/tools.py`
  - 已有真实/半真实工具：
    - `git.status`
    - `git.diff`
    - `test.run`
    - `build.mock_plan`
    - `volcengine.plan`

### Toy Service

- `examples/hello-crazy-api/app.py`
  - `health()`
  - `version()`
  - 如果安装 FastAPI，也可作为 FastAPI app 运行。

- `examples/hello-crazy-api/tests/test_app.py`
  - 当前 toy service 测试已通过。

- `examples/hello-crazy-api/Dockerfile`
  - 第一版只生成 build plan，不强制真实 build。

- `examples/hello-crazy-api/crazy.yml`
  - CI/CD world 配置落脚点。

## 待手写模块

当前脚手架仍有以下 4 个故意红灯，它们只代表最初的文件槽位，不再代表学习顺序。

| 原槽位 | 正式源码 | 参考实现 | 测试 |
|---:|---|---|---|
| 1 | `crazy_harness/core/a2a/bus.py` | `reference_implementations/a2a_bus.py` | `python -m pytest -q tests/core/test_a2a_bus.py` |
| 2 | `crazy_harness/core/prompts/contract.py` | `reference_implementations/prompt_contract.py` | `python -m pytest -q tests/core/test_prompt_pack.py` |
| 3 | `crazy_harness/core/context/microcompact.py` | `reference_implementations/microcompact.py` | `python -m pytest -q tests/core/test_microcompact.py` |
| 4 | `crazy_harness/core/agents/loop.py` | `reference_implementations/agent_loop.py` | `python -m pytest -q tests/core/test_agent_loop.py` |

修订后的学习顺序从 `Agent Loop` 开始：

1. 先保留一个真实可运行的朴素 loop baseline。
2. 扩写 `AgentLoop` 测试，使其覆盖 phase、typed command、持久记录和崩溃恢复，而不是只把当前薄测试变绿。
3. 你手写第一章核心状态机；Prompt、Context、Tool、Hook、Event 先通过稳定端口接入。
4. 下一章做 Goal、Exit Criteria、LocalPlan、Progress、Budget、Nudge 和完成门槛。
5. 常驻运行时稳定后，再做持久 `A2ABus`；Context 与 Prompt 章节随后分别深化。

原因：

- A2A 是多个 Agent Loop 之间的受控通信；单个 Loop 的控制权与恢复语义不稳时，先写 Bus 只会得到消息容器。
- Context、Prompt、Tool 和 Hook 的深层机制都需要一个真实 Loop 承载，先有内核才能观察它们解决的实际失败。
- 原参考实现仅用于理解旧薄接口，不是第一章 hardened loop 的最终答案；正式实现与测试会在章前设计后增量扩展。

完整章节依赖和准出条件见 `docs/HARNESS_MASTERY_ROADMAP.md`。

## 当前验证状态

2026-07-10 完整状态：

```powershell
python -m pytest -q
```

结果：6 passed、1 skipped、4 failed。4 个失败均来自故意保留的 `HANDWRITE_TODO`，不是本轮文档修改引入的回归。

基础绿灯：

```powershell
python -m pytest -q tests\core\test_event_log.py tests\core\test_artifact_store.py tests\core\test_tool_registry.py tests\worlds\cicd\test_artifact_schemas.py tests\worlds\cicd\test_cicd_tools.py
```

结果：5 tests passed。

Toy service：

```powershell
cd examples\hello-crazy-api
python -m unittest discover -s tests
```

结果：2 tests passed。

当前预期红灯：

```powershell
python -m pytest -q tests\core\test_prompt_pack.py tests\core\test_a2a_bus.py tests\core\test_microcompact.py tests\core\test_agent_loop.py
```

结果：4 tests failed，均为预期 `HANDWRITE_TODO`。

## 需要后续清理的平行草稿

当前目录里有少量平行草稿文件：

- `crazy_harness/core/a2a/messages.py`
- `crazy_harness/core/prompts/pack.py`
- `crazy_harness/core/context/builder.py`
- `crazy_harness/core/models/base.py`
- `crazy_harness/core/models/fake.py`
- `crazy_harness/core/models/deepseek.py`
- `crazy_harness/core/events/models.py`
- `crazy_harness/core/artifacts/models.py`

当前主线以这些文件为准：

- `crazy_harness/core/a2a/bus.py`
- `crazy_harness/core/prompts/contract.py`
- `crazy_harness/core/context/microcompact.py`
- `crazy_harness/core/agents/loop.py`
- `crazy_harness/core/models/providers.py`
- `crazy_harness/core/events/schemas.py`
- `crazy_harness/core/artifacts/schemas.py`

建议等 4 个核心红灯变绿后，再统一清理或合并平行草稿，避免现在打断手写节奏。
