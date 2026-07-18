# 文档地图 / Documentation Map

Crazy 的公开文档按“先看运行事实，再看机制，再看路线”组织。代码和 Event Trace 是事实源，文档用于解释，不替代测试。

| 顺序 | 文档 | 解决的问题 |
|---:|---|---|
| 1 | [架构走读](ARCHITECTURE_WALKTHROUGH.md) | 常驻 Control Plane、AgentLoop、Mailbox、Context 与 A2A 如何连接？ |
| 2 | [Harness 核心机制](HARNESS_CORE_ESSENTIALS.md) | Loop、Context、Memory、Tool、A2A、Eval 各自控制什么？ |
| 3 | [Resident A2A Checkpoint](RESIDENT_A2A_V01_CHECKPOINT.md) | 当前版本真实做到哪里，哪些仍是边界？ |
| 4 | [Durable Supervisor 走读](DURABLE_SUPERVISOR_WALKTHROUGH.md) | Supervisor 如何动态派活，Lease 如何恢复、续约、过期和切换备用 Agent？ |
| 5 | [Evidence Research TaskPack](EVIDENCE_RESEARCH_TASKPACK.md) | 同一 Runtime 如何换业务壳，并用浏览器证据、引用和 Hash 门禁准出？ |
| 6 | [Agent Skills 渐进披露](AGENT_SKILLS_PROGRESSIVE_DISCLOSURE_WALKTHROUGH.md) | metadata、正文和资源如何按需进入 Context？ |
| 7 | [MCP 延迟发现](MCP_DELAYED_DISCOVERY_WALKTHROUGH.md) | 远端工具如何授权挂载、搜索、披露和执行？ |
| 8 | [16 小时实码学习手册](HARNESS_16H_ACTUAL_CODE_LEARNING_GUIDE.md) | 如何从最小 Loop 调试到 Context、A2A、Memory 与 Eval？ |
| 9 | [通用 Agent Team 主计划](GENERAL_AGENT_TEAM_MASTER_PLAN.md) | 项目如何从学习 Runtime 演进为通用平台？ |
| 10 | [第三方许可证矩阵](THIRD_PARTY_LICENSE_MATRIX.md) | 哪些是协议兼容、模式借鉴或可选依赖？ |

## 阅读原则

- **模型提出动作，Harness 产生事实。** Model Response 通过 Schema、Policy 和预算校验后才成为 Command。
- **Context 每轮编译。** 大结果 Offload，每轮 Microcompact，接近预算再 Full Compact。
- **A2A 共享任务事实，不共享完整私有上下文。** Assignment、Evidence Ref 和 Result 经过持久 Mailbox 传递。
- **自进化只提交 Candidate。** Eval、版本门和 Rollback 决定是否晋升。

完整可运行实验位于 [`../labs/16h_sprint`](../labs/16h_sprint)，公开仓边界见 [Public Repository Manifest](PUBLIC_REPO_MANIFEST.md)。
