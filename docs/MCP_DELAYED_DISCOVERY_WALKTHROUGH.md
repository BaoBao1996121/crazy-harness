# MCP 延迟发现纵切：从目录到事实

一句话结论：**MCP 负责标准化远端能力协议，Harness 决定哪些能力可见、能否执行，以及执行结果何时成为事实。**

## 1. 它解决的不是“能不能调用”

全量加载少量 MCP Tool 时很直接，但 Tool 数量增长后，完整名称、描述和 JSON Schema 会长期占用 Context。当前实现把问题拆成三层：

| 层 | 看见什么 | 谁控制 |
|---|---|---|
| Catalog / 能力目录 | 名称、短描述、标签、kind、provider | Harness |
| Manifest / 本轮披露 | 当前轮允许模型看见的完整 Tool Schema | CapabilityCompiler |
| Execution / 实际执行 | 已校验 Command 对应的远端 tools/call | ToolPipeline + MCP Adapter |

所以，**被搜索召回不等于获准执行；获准可见也不等于工具已经产生事实。**

## 2. 三轮真实链路

~~~mermaid
sequenceDiagram
    participant S as "Scheduler / 调度器"
    participant L as "AgentLoop / 智能体循环"
    participant C as "CapabilityCompiler / 能力编译器"
    participant M as "Model / 模型"
    participant P as "ToolPipeline / 工具管线"
    participant A as "MCP Adapter / 协议适配器"
    participant R as "FastMCP Server / 远端服务"
    S->>L: "Wake turn 1 / 唤醒第 1 轮"
    L->>C: "Compile authorized manifest / 编译授权清单"
    C-->>L: "capability.search only / 只披露搜索工具"
    L->>M: "Context + native schemas / 上下文与原生 Schema"
    M-->>L: "call capability.search / 建议搜索"
    L->>P: "Validated Command / 已校验命令"
    P-->>L: "short metadata + source event / 短元数据与来源事件"
    S->>L: "Wake turn 2 / 唤醒第 2 轮"
    L->>C: "Recall search evidence / 读取搜索证据"
    C-->>L: "add mcp.docs.lookup schema / 披露远端工具 Schema"
    L->>M: "Recompiled Context / 重新编译上下文"
    M-->>L: "native call mcp.docs.lookup / 原生工具调用"
    L->>P: "Policy, Hook, Ledger / 鉴权、钩子、账本"
    P->>A: "local name -> remote name / 本地名映射远端名"
    A->>R: "MCP tools/call"
    R-->>A: "content + structuredContent + _meta"
    A-->>P: "model-visible result, no _meta / 可见结果，不含 _meta"
    P-->>L: "tool.completed fact / 工具完成事实"
    S->>L: "Wake turn 3 / 唤醒第 3 轮"
    L->>M: "Evidence in Context / 证据进入上下文"
    M-->>L: "stop proposal / 停止建议"
    L-->>S: "agent.stopped / Harness 确认停止"
~~~

多出的搜索轮次是在用**调用次数换 Context 空间**。目录很小时，CapabilityCompiler 直接 inline all，不需要这次搜索。

## 3. 五个关键对象

| 对象 | 责任 | 不能负责什么 |
|---|---|---|
| MCPClientPort | 定义 list_tools 与 call_tool 的稳定接口 | 不决定权限 |
| SDKSessionMCPClient | 使用官方 SDK 完成初始化、tools/list、tools/call | 不决定模型看见什么 |
| MCPToolGrant | 本地权限真相源，声明允许挂载的远端 Tool | 不相信服务端 annotations 自动扩权 |
| MCPToolMount | 命名空间、注册、刷新和远端结果转换 | 不绕过 ToolPipeline |
| CapabilityManifest v2 | 记录本轮披露名称、kind、provider 与召回来源 | 不等于执行授权 |

本地名使用 mcp.<server>.<tool>。例如远端 docs 服务的 lookup，在模型侧叫 mcp.docs.lookup。这样可以避免不同 Server 的同名 Tool 相互覆盖，也便于审计 provider。

## 4. 为什么不做一个万能 mcp.call

如果只给模型一个 mcp.call(name, args)：

1. 模型拿不到每个 Tool 的原生参数 Schema，参数更容易错。
2. 所有 MCP 调用在 Trace 中都长得一样，难以做工具级权限与评估。
3. Provider 身份、并发属性和副作用策略更容易被包装层抹平。
4. 模型训练和 API 对原生 Tool Calling 的结构化约束无法充分利用。

当前路线是：**延迟发现决定何时给 Schema，真正调用仍保持原生 Tool 身份。**

## 5. 信任边界

~~~mermaid
flowchart LR
    U["Remote server metadata<br/>远端服务元数据"] --> G["Local Grant<br/>本地授权"]
    G --> D["Catalog stub<br/>目录短元数据"]
    D --> S["capability.search<br/>能力搜索"]
    S --> E["Persisted source event<br/>持久召回证据"]
    E --> M["Manifest disclosure<br/>本轮 Schema 披露"]
    M --> V["Command validation<br/>命令校验"]
    V --> P["Policy + Hook + Ledger<br/>策略、钩子、账本"]
    P --> C["MCP tools/call<br/>远端调用"]
    C --> O["tool.completed<br/>工具事实"]
~~~

需要背住：**Server 告诉我们“它有什么”，本地 Grant 决定“我们信任并允许什么”，Manifest 决定“模型这轮看见什么”，ToolPipeline 决定“这次能不能做”。**

## 6. _meta 为什么要删

MCP CallToolResult 可以同时包含：

- content：文本、图片等模型可见内容；
- structuredContent：符合输出 Schema 的结构化结果；
- _meta：只给客户端或 UI 的附加信息。

_meta 可能含内部游标、调试信息或敏感实现数据。Adapter 会递归删除 _meta，只把前两类写入模型 Observation。协议对象能被客户端读到，不代表都应该进入 Context。

## 7. 崩溃与副作用

MCP 调用仍然遵守既有恢复原则：

- model.completed 已持久化：复用 Response，不重复调用模型；
- operation.started 后没有 operation.completed：状态是 UNKNOWN；
- 只有能够证明远端调用未生效，或 Tool 明确幂等时，才可自动重试；
- 否则先通过业务事实或幂等键对账。

MCP 统一了协议，不会自动提供分布式 exactly-once。

## 8. 当前实现的真实边界

已经真实运行：

- 官方 FastMCP memory transport；
- MCP 初始化、tools/list 分页、tools/call；
- 本地 Grant、命名空间与动态 refresh；
- Tool Search、下一轮 Schema 披露、原生调用；
- 持久 Mailbox、Scheduler、AgentLoop、EventStore；
- Control Room 中的 provider、kind 与召回来源显示。

尚未完成：

- 外部 stdio 与 Streamable HTTP 长连接；
- OAuth、断线重连和连接池；
- notifications/tools/list_changed 订阅；
- 长连接 Session 的并发与背压；
- 外部 MCP Server 的故障注入和性能基准。

因此准确说法是：**首个 MCP 真实协议纵切已完成，生产级外部 Transport 尚未完成。**

## 9. 调试入口

运行：

~~~powershell
python work\run_mcp_capability_demo.py
~~~

重点看：

1. 第一份 capability.manifest.compiled 中没有 mcp.docs.lookup。
2. capability.search 的 tool.completed 返回短元数据和 Event ID。
3. 下一份 Manifest 出现 mcp.docs.lookup、kind=mcp、provider=mcp:docs。
4. operation.started 之后才发生官方 tools/call。
5. tool.completed 中有 content/structuredContent，没有 _meta。
6. 最终由 Harness 记录 agent.stopped 与 run.succeeded。

Control Room 可打开：

~~~text
http://127.0.0.1:8765/?run=run_6ead89282b80
~~~

## 10. 面试版表达

> 我没有把 MCP 全量 Schema 常驻上下文，也没有用万能 mcp.call 包装器。Harness 先用本地 Grant 过滤远端目录，只让模型搜索授权短元数据；搜索结果作为持久证据，使完整原生 Schema 在下一轮进入 Manifest。之后调用仍经过 Command Validation、ToolPolicy、Hook、OperationLedger 和预算。官方 MCP SDK 只负责协议，权限与事实边界仍由 Harness 掌握。
