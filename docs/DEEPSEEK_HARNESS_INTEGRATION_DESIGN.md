# DeepSeek Harness Integration Design

> 状态：v1 tracer bullet 已实现。基线为官方 `dsh-v0.1.0-rc.7`
> (`99f6f02fecdb7dff40c3fbc9470f5907c29f74ca`)。DSH 仍处于 Developer
> Preview，本设计把升级兼容性视为必须持续验证的合同，而不是一次性承诺。

## 决策

Crazy 采用“官方 DSH + 树外 Cordis Bundle + versioned HTTP sidecar”架构：

- DSH 拥有唯一交互式 Agent Loop、Session、Tool Registry/UX、Approval 调用
  生命周期与插件装配。
- Crazy 不再扩建第二套通用 Harness 主循环；现有 Engineering Loop、Event、
  A2A、Eval、Artifact、Scientific Job、Portfolio 等能力作为可组合业务部件。
- Crazy 的持久 ledger 继续拥有跨会话业务事实；DSH Session 只保存调用结果、
  摘要和 opaque reference。
- 不 vendor DSH，不使用 submodule，不修改 `@deepseek-ai/dsh-agent-loop`，默认
  不 fork。只有上游确实缺失必要扩展点时，才允许临时薄 fork 并优先回馈上游。

这一边界让 DSH 的 Agent、Context、Tool、Subagent、Job、UI 等更新可以通过
升级官方 npm 包获得；Crazy 只维护自己明确拥有的业务合同。

## 所有权

| 能力 | DSH 所有 | Crazy 所有 | 接入方式 |
|---|---|---|---|
| Agent Loop / Session | Turn、模型、工具调用、Session log | 无第二套交互主循环 | 官方 DSH 原生 |
| Engineering Loop | 展示和发起工具调用 | Candidate、child Run、Eval、Decision、晋升事实 | versioned HTTP tool |
| Scientific Job | 当前 Session 的 Job 镜像 | 跨时段 ledger、重试、对账、Portfolio | 后续 Job provider |
| Artifact | Attachment 预览、opaque ref | 原始对象、hash、provenance | 后续 tool/job |
| Approval | 当前危险 Tool Call | 跨时段科学 Gate | 两个不同生命周期 |
| A2A | Subagent 生命周期和 UX | Remote/业务委派 ledger | 后续命名 `SubagentProvider` |
| Event | DSH Session event | 业务 Event 与少量关联投影 | 后续幂等镜像 |

禁止 DSH 直接访问 Crazy SQLite、Python 对象、内部权限对象或 raw filesystem
URI。跨进程 DTO 必须显式版本化、收窄并 fail closed。

## 当前实现

目录 `integrations/deepseek-harness` 是独立 pnpm workspace，包含四个拆件：

```text
dsh-control-plane              Service Definition
dsh-control-plane-http         loopback HTTP Provider
dsh-tool-engineering-loop      read-only Tool Consumer
dsh-bundle                     patch-only composition
```

Crazy Control Plane 暴露：

```text
GET /api/integrations/dsh/v1/capabilities
GET /api/integrations/dsh/v1/engineering-loops/{loop_id}
```

握手为 `crazy-dsh-v1` / `http-json`，当前只声明
`engineering_loop.read`。Loop DTO 不暴露 Contract、Permissions、Worker、Metric、
State Ref 或推进命令。Tool 明确不能 advance、drain、pause、resume 或 cancel。

Provider 只接受 `http://127.0.0.1`、`http://localhost` 或 `http://[::1]`，拒绝
URL 凭据、query、fragment 与 HTTP redirect；所有 wire response 经 Zod 严格验证。
响应正文按实际 byte 流统一限制为 16 KiB，并先拒绝声明超限的 Content-Length；
Pack、CLI 与完整 boot 都在无 shell 的独立进程组中运行：CLI 截止为 10 秒，boot
截止为 20 秒；超时会在 Windows 终止精确 PID 子树，在 POSIX 终止对应进程组，
避免上游生命周期漂移把后台进程遗留到整个 CI job。
v1 不是远程网络协议。若以后跨主机，必须增加独立 listener 或保护完整 `/api`
面，并配套 TLS/mTLS、认证与调用方隔离，不能只给新路由补 Bearer Token。

## 组合合同

Bundle 只包含以下 Patch：

```yaml
- insert:
    - id: crazy-control-plane-http
      name: '@crazy-harness/dsh-control-plane-http'
    - id: crazy-tool-engineering-loop
      name: '@crazy-harness/dsh-tool-engineering-loop'
```

官方 CLI 契约测试会先对四个拆件执行 `pnpm pack`，再在隔离 `DSH_HOME` 中
初始化 profile。由于这些私有包尚未发布，测试只在临时 profile 的
`pnpm-workspace.yaml` 中把 Bundle 声明的三个精确版本映射到本轮 tarball；打包后
manifest 保持未来 registry 安装所需的普通版本依赖，不被测试改写。profile 只
显式安装 Bundle tarball，然后执行 `--dump-config` 并通过官方 `dsh-app-boot`
启动完整 base profile，要求：

- profile manifest 只有 Bundle 这一项 Crazy 直接依赖，三个运行拆件由 Bundle
  依赖安装并可被 Loader 解析；
- `agent-loop` 恰好一行，仍指向 `@deepseek-ai/dsh-agent-loop`；
- Crazy 两行只追加在最终组合末尾；
- Bundle Patch 的任何位置都不得出现 `agent-loop`；
- boot 后 `ctx.crazyControlPlane` 与
  `crazy_engineering_loop_get` 同时存在，释放 Context 后生命周期完整结束。

Bundle 本身是 patch-only，不提供 runtime main。Provider 提供
`ctx.crazyControlPlane`，Tool 通过 Cordis injection 等待 `tools` 与该服务；二者
不通过 Bundle 聚合代码形成隐藏耦合。

测试 profile 仍保持 `--offline` 合同，并显式把 workspace 的 effective pnpm
store 通过 `PNPM_CONFIG_STORE_DIR` 传给官方 CLI；每次测试使用冷 `PNPM_HOME`，所以
临时 profile 跨盘时不会悄悄切到空 store 或依赖开发机缓存。

## 版本与更新红利

生产基线精确锁定 `@deepseek-ai/dsh@0.1.0-rc.7` 和
`@deepseek-ai/dsh-tools@0.1.0-rc.7`。内部 DSH 包的 npm `latest` 曾落后于
`next`，因此不得用 dist-tag 或宽 semver 作为生产合同。

`pnpm dsh:check-pins` 会扫描 workspace 根和 `packages/*` 四种依赖区段中的
全部 `@deepseek-ai/dsh*` 直依赖；当前 8 个 pin 必须与根包基线完全一致。
`pnpm dsh:pin <exact-version>` 使用同一发现逻辑更新版本，避免新增拆件后只改到
手工清单的一部分。

升级必须作为显式依赖 PR：

1. 核对官方 tag、commit、release notes 与扩展文档；
2. 同时更新所有直接 DSH pin 和 lockfile；
3. 审查新增 lifecycle/build script；`strictDepBuilds` 让未分类脚本直接阻断安装，
   当前仅允许 rc.7 已审查的 `node-pty`、
   `koffi`、`dsh-subprocess-local`，拒绝 `@google/genai` 与 `protobufjs`，任何
   新脚本默认拒绝；
4. 运行 Python façade、TS unit/contract、TypeScript build 和真实
   `--dump-config` 组合门禁；
5. 记录迁移决策、破坏性变化与回退 pin；
6. 门禁失败时继续使用上一个已验证 pin，不在生产分支追随 `next`。

主 CI 的 `dsh-integration` job 已配置 frozen lockfile，并覆盖 Ubuntu/Node 22.19 与
Windows/Node 24。`dsh-next-probe` 在相同矩阵中，仅于 schedule 或手动触发时把
npm `next` 应用到临时 checkout 并执行相同门禁；它不是依赖自动晋升，也不会
覆盖提交中的精确 pin。探针不读写按提交中 rc.7 lockfile 建立的 pnpm cache，
避免未审查的 `next` 依赖闭包污染主 CI。run `32102216325` 已在本分支实跑并
通过 Ubuntu/Node 22.19、Windows/Node 24 DSH 以及 Ubuntu/Windows Python backend
和 frontend 五项门禁；这证明的是 rc.7 生产 pin 的跨平台合同，不等同于不同
`next` 版本已经兼容。

## 后续路线

1. Engineering Loop 从只读扩到受控 submit/cancel，先冻结幂等键、Profile
   allowlist 与稳定错误模型。
2. 增加 Scientific Job provider，DSH `ctx.jobs` 只保存会话镜像，Crazy ledger
   保持权威。
3. 增加 Artifact ref/preview 与 Evaluation tool，所有结果绑定 hash/provenance。
4. 实现命名 SubagentProvider 和少量 `crazy/*` Event ref 镜像，避免复制完整
   DSH Session log。
5. 最后接跨时段 ApprovalTask、Portfolio 与真实 Scientific Loop；每一层先有
   keyless contract test，再接付费模型或外部副作用。

## 已验证证据

- 本机 Node 24.19：frozen install 通过；`pnpm test` 先验证 8 个 DSH 精确 pin，
  再完成 TypeScript project build 和 `5 files / 14 tests`。
- `pnpm test` 覆盖 HTTP Provider、Tool Consumer、最小 Cordis lifecycle，以及
  pack 后 Bundle 在隔离 profile 中的 CLI 安装、配置组合和官方 base profile
  完整 boot；官方 `agent-loop` 唯一，Crazy Service/Tool 均已激活并释放；真实
  父子 Node 进程树超时探针验证了硬截止清理路径。
- Python façade：聚焦 Pytest 为 `1 passed, 7 deselected`，相关 Ruff 通过。
- TypeScript build 未启用 `skipLibCheck`；同步后的前端 production build 此前已在
  同一 OpenAPI 语义下通过，本轮未因测试夹具和门禁修改重复运行。
- 远端 run `32102216325` 五项 job 全部成功；首次 run `32100717131` 的 Windows DSH
  `ERR_PNPM_NO_OFFLINE_TARBALL` 已由 `45f3e57` 的显式
  `PNPM_CONFIG_STORE_DIR`/冷 `PNPM_HOME` 合同修复。该修复只在 Crazy 测试边界内，
  未修改 DSH 上游。

本地 tarball 安装合同已经验证，但 registry 发布、SBOM/NOTICE、真实模型调用、
远程认证、Scientific Job、A2A Provider 与 Event mirror 仍未完成，不能从当前
tracer bullet 推断这些能力已经可用。
