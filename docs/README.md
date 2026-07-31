# 文档地图 / Documentation Map

Crazy 的公开文档按“先看运行事实，再看机制，再看路线”组织。代码和 Event Trace 是事实源，文档用于解释，不替代测试。

| 顺序 | 文档 | 解决的问题 |
|---:|---|---|
| 1 | [当前平台架构学习指南](CURRENT_PLATFORM_ARCHITECTURE_LEARNING_GUIDE.md) | 先建立当前 As-Built 全景：六层架构、五种 Loop、持久事实、Single/Team、治理模块与成熟度分别是什么？ |
| 2 | [架构走读](ARCHITECTURE_WALKTHROUGH.md) | 当前常驻 Control Plane、Assignment/Peer child AgentRun、canonical AgentLoop 与 A2A 如何连接？ |
| 3 | [Durable Supervisor 走读](DURABLE_SUPERVISOR_WALKTHROUGH.md) | Supervisor 如何动态派活，Team Worker 如何 Wait/Resume，Kernel 如何晋升结果并传播失败？ |
| 4 | [受控并发与 Fencing](CONTROLLED_CONCURRENCY_WALKTHROUGH.md) | 容量、背压、Claim、续期、取消和旧 Worker 隔离如何真实运行？ |
| 5 | [在线 Team 模型治理](ONLINE_TEAM_MODEL_GOVERNANCE_WALKTHROUGH.md) | child AgentRun 如何独立路由模型，付费请求如何预约、重试、核销和恢复？ |
| 6 | [Single vs Team 公平评测](SINGLE_VS_TEAM_EVAL_DESIGN.md) | 如何用 Prepare/Commit、独立 Scorer、完整模型证明与保守推荐，得到可恢复且可解释的公平对照？ |
| 7 | [Composite Checkpoint](COMPOSITE_CHECKPOINT_DESIGN.md) | 如何冻结 Workspace、状态引用、Artifact 和 Effect 边界，并从同一请求跨崩溃 Fork Restore？ |
| 8 | [Durable Engineering Loop](DURABLE_ENGINEERING_LOOP_DESIGN.md) | 外层 Candidate 如何运行真实 child Agent、经独立评测后晋升并把 Active 传给下一轮；它与单 Run 控制有什么区别？ |
| 9 | [Harness 核心机制](HARNESS_CORE_ESSENTIALS.md) | Loop、Context、Memory、Tool、A2A、Eval 各自控制什么？ |
| 10 | [通用 Agent Team 主计划](GENERAL_AGENT_TEAM_MASTER_PLAN.md) | 当前实现位于哪一阶段，如何继续演进为通用平台？ |
| 11 | [Evidence Research TaskPack](EVIDENCE_RESEARCH_TASKPACK.md) | 同一 Runtime 如何换业务壳，并用浏览器证据、引用和 Hash 门禁准出？ |
| 12 | [Agent Skills 渐进披露](AGENT_SKILLS_PROGRESSIVE_DISCLOSURE_WALKTHROUGH.md) | metadata、正文和资源如何按需进入 Context？ |
| 13 | [MCP 延迟发现](MCP_DELAYED_DISCOVERY_WALKTHROUGH.md) | 远端工具如何授权挂载、搜索、披露和执行？ |
| 14 | [16 小时实码学习手册](HARNESS_16H_ACTUAL_CODE_LEARNING_GUIDE.md) | 如何从最小 Loop 调试到 Context、A2A、Memory 与 Eval？ |
| 15 | [第三方许可证矩阵](THIRD_PARTY_LICENSE_MATRIX.md) | 哪些是协议兼容、模式借鉴或可选依赖？ |
| 16 | [Resident A2A v0.1 历史 Checkpoint](RESIDENT_A2A_V01_CHECKPOINT.md) | 最初 MVP 如何定界？仅用于理解演进历史，不代表当前实现。 |
| 17 | [Anew Scientific Harness 差距与路线](ANEW_SCIENTIFIC_HARNESS_GAP_ROADMAP.md) | 当前通用 Harness 如何映射到 Anew 类科研平台，第一条真实科学纵切为什么选候选批量初筛？ |

阅读状态时优先相信当前源码、测试与前四份文档。`RESIDENT_A2A_V01_CHECKPOINT.md` 已冻结为历史快照，其中的范围和准出条件只描述当时阶段。

## 阅读原则

- **模型提出动作，Harness 产生事实。** Model Response 通过 Schema、Policy 和预算校验后才成为 Command。
- **Context 每轮编译。** 大结果 Offload，每轮 Microcompact，接近预算再 Full Compact。
- **A2A 共享任务事实，不共享完整私有上下文。** Assignment、Evidence Ref 和 Result 经过持久 Mailbox 传递。
- **自进化只提交 Candidate。** Eval、版本门和 Rollback 决定是否晋升。

完整可运行实验位于 [`../labs/16h_sprint`](../labs/16h_sprint)，公开仓边界见 [Public Repository Manifest](PUBLIC_REPO_MANIFEST.md)。
