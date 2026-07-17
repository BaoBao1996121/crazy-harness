# Third-Party License Matrix

> 更新日期：2026-07-17。状态为“待锁定”的组件不能进入默认安装依赖，必须在集成 PR 中固定版本、许可证和来源 Commit。

| 项目 | 计划用途 | 集成级别 | 已知许可证 | 当前状态 |
|---|---|---|---|---|
| OpenClaw | Gateway、隔离、安全模式参考 | Pattern | MIT | 已研究，不复制源码 |
| Hermes Agent | Completion Contract、Learning Lane、PTC 参考 | Pattern | MIT | 已锁定过源码快照 |
| OpenHands SDK | 类型 Event、Workspace 契约参考 | Pattern/optional adapter | MIT | 版本待集成时锁定 |
| mini-SWE-agent | Thin Loop 与 Eval baseline | Dev/Eval optional | MIT | 版本待集成时锁定 |
| Anthropic Agent Skills | Skill 规范兼容 | Protocol compatibility | 仓库许可证待发布前复核；本项目自建示例为 Apache-2.0 | name/description/frontmatter 与渐进披露已实现；不复制上游示例 Skill |
| MCP Python SDK | MCP Client/Server | Optional protocol dependency | MIT | 已锁定 `mcp>=1.27,<2`；本机验证 1.27.2，v2 预发布不采用 |
| PyYAML | Agent Skills frontmatter 安全解析 | Core dependency | MIT | 已锁定 `PyYAML>=6.0,<7`；本机验证 6.0.3，只使用 `safe_load` |
| A2A SDK | Remote Agent Adapter | Protocol dependency | Apache-2.0，待 SDK 复核 | 版本待集成时锁定 |
| agentgateway | 可选生产连接代理 | Optional sidecar | Apache-2.0 | 不进入默认安装 |
| Playwright | 确定性 BrowserRuntime | Optional dependency | Apache-2.0 | 当前已使用 |
| Browser Use | 开放式 Browser Worker | Optional adapter | MIT，待版本复核 | 不接管主 Loop |
| Daytona | 云 SandboxRuntime | Optional adapter | 待集成 PR 复核 | 不进入默认安装 |
| E2B | 云 SandboxRuntime | Optional adapter | 待集成 PR 复核 | 不进入默认安装 |
| Hindsight | MemoryProvider A/B | Optional adapter | MIT | 已锁定过源码快照 |
| Letta Code | Git-backed Context Pattern | Pattern | Apache-2.0 | 不作为 Memory 真相源 |
| Graphiti | 时序 MemoryProvider | Optional adapter | Apache-2.0 | 后置实验 |
| OpenTelemetry | TraceExporter | Protocol dependency | Apache-2.0 | 版本待集成时锁定 |
| Langfuse | Trace/Eval UI | Optional exporter | MIT，部署依赖另审 | 不作为正确性依赖 |
| Inspect AI | EvalProvider Bridge | Dev/Eval optional | MIT | 版本待集成时锁定 |
| DSPy/GEPA | Offline optimizer | Dev/Eval optional | 待集成 PR 复核 | 只能产生 Candidate |

## 合规规则

1. 所有直接依赖必须在 lockfile、SBOM 和 NOTICE 中可追踪。
2. Pattern 借鉴只写行为规格与自主实现，不复制 Prompt、测试表达或内部 Schema。
3. AGPL 代码不得复制或链接进 Apache-2.0 核心；需要时只能作为独立外部服务并单独评审。
4. Adapter PR 必须附官方仓库、版本/Commit、许可证文件 Hash 和最小权限说明。
5. 项目名、Logo、截图和商标不因开源许可证自动获得使用权。
