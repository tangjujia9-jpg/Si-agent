# Si-agent 第五周开发与验收报告

本轮把前三、四周的项目记忆接入自主 Agent Loop，并实现模型适配、工具执行边界、
检查点、审批恢复和可选 Langfuse 导出。产品仍采用单用户多项目，旧 CLI 独立运行。

## 1. 从检索服务走向可执行 Agent

此前 API 能导入、搜索和检查资料。现在 API 可以接受会话消息，并在事务内保存
用户消息、Run 和 Worker Job。Worker 使用项目记忆和模型工具调用完成运行，
把最终回答写回会话。客户端断开 SSE 连接不会取消后台任务。

```text
提交消息 → 持久化 Run/Job → Worker 领取租约
  → Retrieval Gate → 分层混合检索 → 带引用的 Context Compiler
  → Provider → 工具计划 → 入参/能力校验 → 工具结果 → 下一轮模型
  → 回答入库 / 等待审批 / 失败 / unknown / 取消
```

默认 `demo/evidence-demo` 只验证证据链和运行链，不生成模型推理答案。
真实模型须在后端配置密钥，并在请求中指定 provider/model。

## 2. Harness 控制执行边界

| 能力 | 实现和作用 |
|---|---|
| 预算 | 限制迭代、工具次数、token 预留和绝对截止时间；调用前持久化预留 |
| 重试 | 主模型仅为 429、部分 5xx、连接和超时错误做一次重试；SDK 隐式重试关闭 |
| 幂等请求 | 同一会话的同一键复用 Run；输入变化返回 409；同会话只允许一个活动运行 |
| 动态工具 | 每次运行只暴露允许的工具；Pydantic 严格检查类型并拒绝额外字段 |
| 检查点 | JSON 保存消息、模型、工具计划、进度、来源、预算和截止时间 |
| 工具结果复用 | 操作 ID、调用 ID 和结果持久化；恢复时读取已知结果 |
| 审批与 outbox | 写操作先等待用户审批；批准、任务与 outbox 在同一事务提交 |
| 不确定动作 | 非幂等写操作中断后停止为 unknown，不自动重放；需人工检查资源 |
| 任务耗尽 | Worker 重试耗尽时同步结束 Run，避免运行永久卡在等待状态 |
| 项目隔离 | 入口、检索、工具及恢复都核查项目所有者；检索资料不能提供工具授权 |

`charged_tokens` 是保守的预算预留，包含失败尝试，不是实际收费 token。
`usage_tokens` 是主模型 SDK 返回的实际用量；辅助模型的用量记录在对应 span。
未配置厂商价格时不推测美元成本。实际主模型请求最多等待 20 秒，并受运行剩余时间约束。
外部动作的“恰好一次”还需要目标服务接受 operation ID 作为幂等键，单靠数据库不能保证。

首批产品工具为 `search_memory`、`read_memory`、`propose_memory`。
第三个工具只能保存待审候选；允许工具执行和接受长期记忆是两个不同的审批步骤。
API 没有直接暴露旧 CLI 的任意 shell、日历或系统操作。

## 3. Provider 接入保持模型无关

OpenAI、Anthropic 和 OpenAI-compatible 适配器共享 JSON 消息、工具定义、响应与 usage。
适配器转换各厂商的 tool call/result 格式，同时支持原生流式协议。兼容服务使用
`max_tokens`；OpenAI Chat Completions 使用 `max_completion_tokens`。

CredentialResolver 只读取装配入口传入的后端环境配置；ModelCatalog 返回可序列化
模型与能力，`GET /api/models` 不返回密钥。上下文窗口等能力可由部署配置明确指定，
未知窗口采用保守值，不凭模型名推断视觉、reasoning 或价格。

产品运行时本轮采用 complete 请求；SSE 展示持久化过程事件和最终快照。
Provider 的流片段已测试，聊天 token delta 的产品接线和前端展示进入第六周。

## 4. Jev 接在决策环节

核对对象是 TypeSafe 的 Jev / System One，使用固定版本 `typesafe-sdk==0.7.4`。
它提供分类、概率和置信度；本项目采用 Choice 返回结构。它不是这里自定义的 Judge 缩写。

| 接入点 | 本轮状态 | 判断与保守处理 |
|---|---|---|
| 用户问题进入检索前 | 已接入 | 判断是否依赖项目资料或历史决策；低置信度和错误继续检索 |
| 候选记忆抽取后、接受前 | 已接入 enrich Worker | 判断是否是值得长期保存的稳定事实；高置信度否定跳过，其余待审 |
| 混合召回后、上下文组装前 | relevance 协议已定义，未启用排序 | 第七周比较质量、调用成本和阈值后决定 |
| 工具批准、冲突、忘记 | 由程序和用户处理 | 不让概率分数覆盖授权、证据、版本或 tombstone |

当前阈值为置信度 0.8，二分类 yes 概率阈值为 0.5，仅是可验证的初始策略。
本轮没有测真实 Jev 对中文项目数据的质量，也不声称它优于本地规则。
默认使用本地规则；显式启用 Jev 后，查询、最近会话片段或候选事实会发往决策服务。
Jev 不生成摘要、改写查询或生成事实文本。

候选留存当前来源于导入资料的 enrich 和 Agent 的 propose 工具。
完整会话的自动蒸馏、时间冲突质量评测与 rerank 需要后续完善。

## 5. Tracing 与 Langfuse

RunEvent 在数据库保存有序事件；SSE 使用序号重放。真实调用产生 span ID、parent ID、
持续时间、错误类型、模型用量和操作身份。检索层级、上下文编译、模型、工具和
后台摘要、抽取、embedding、留存判断都能进入观测链。

Langfuse 通过可选 OTel HTTP exporter 接收同一记录；默认停用。
实现使用独立 TracerProvider，不改写旧 OTel 全局配置；导出队列有界，失败不改变业务结果，
退出时有界 flush。事件属性采用 allowlist 并脱敏，默认不导出 prompt、原文、工具参数或结果。
业务检查点仍会保存运行必需的消息与工具数据，因此“观测脱敏”不等于“数据库没有正文”。

本轮验证了真实 OTLP protobuf 编码、HTTP 路径、Basic 鉴权、父子 ID、duration 和 usage 映射。
尚未连接真实 Langfuse 实例验收 UI、查询或评测分数；LLM judge 和 score 导出仍在第七周。
运行手册见[运行时设计与配置](copilot-runtime.md)。

## 6. 记忆忘记与恢复联动

检查点和回答记录来源节点、revision 和证据 ID，恢复与回答提交前核查有效性。
摘要层使用真实 MemoryEvidence ID，不再把导入 Job ID 混在证据 ID 中。
忘记来源时同时清理依赖它的检查点、会话消息和工具载荷，并取消活动运行。
新会话轮次继承历史来源依赖，避免通过历史回答绕回已忘记内容。

已经发送给外部服务的请求无法撤回；本地删除不代表物理清除备份、任意语义改写或
第三方缓存。正在执行的外部动作也不能由数据库取消保证撤销。

## 7. LangGraph 保持可选

默认运行时不依赖 LangChain 或 LangGraph。可选 workflow 适配器提供 advance、
审批 interrupt、resume 和步数限制；工具、记忆、模型与授权仍属于 Si Runtime。
适配器要求调用者传入 checkpointer。本轮使用 InMemorySaver 验证暂停恢复，
没有把 LangGraph PostgreSQL checkpointer 接成默认产品路径。Si 自主运行时的检查点
已经保存在 PostgreSQL Run 中，两者应分别理解。

## 8. 迁移、测试与后续

迁移 `0005_runtime` 增加运行状态、`run_events`、`tool_operations` 和 `outbox_events`。
升级执行 `alembic upgrade head`；存在未处理操作或暂停运行时拒绝有损降级。
`/ready` 检查新增表，避免使用旧库启动后才在第一条消息上失败。

本机没有可用 Docker daemon，真实 PostgreSQL、pgvector 和 Compose 由 GitHub CI 验证。
本地 Windows 的全量测试存在上游 Linux-only hosted 用例；UTF-8 模式与排除 hosted
后的结果、产品回归及 Linux CI 最终结果记录于下方。

第六周将增加 Chat、token delta、审批 UI、Run Trace 和模型设置。第七周将比较
本地检索门与 Jev、评测记忆质量和成本，并把评测结果接到 Eval Lab/Langfuse。

### 本地验证记录

- `ruff check waku evals scripts hosted` 与 `uv lock --check` 通过。
- 扩大回归使用 `python -X utf8 -m pytest -q evals/deterministic --ignore=evals/deterministic/hosted`，
  初次得到 1052 通过、126 跳过、6 失败。其中 README 品牌要求已改为 Si 身份与上游署名检查；
  其余失败涉及本机 `python3`、设计文件 CRLF 摘要和 Windows sdist 路径，未修改受保护品牌文件。
- 不排除 hosted 的原始全量命令在 Windows 收集期失败：Linux 专用 `os.geteuid` 不存在；
  不开启 UTF-8 模式还会遇到上游默认 GBK 读取问题。Linux CI 执行全仓库确定性测试。
- 产品回归涵盖入库、分层检索、证据、版本、遗忘、运行、审批、恢复、Provider、Jev SDK、
  Langfuse OTLP 与可选 LangGraph；本机真实 PostgreSQL 用例因无服务明确跳过。

- 产品/契约/规则/品牌回归最终为 **107 通过、63 跳过**（Windows，60.72 秒）；没有失败。

- 本机还以独立 API/Worker 进程和临时 SQLite 数据库通过 HTTP 冒烟，验证 19 个运行事件及 SSE。
  临时目录清理初次遇到 Windows 文件锁；测试服务已退出，业务冒烟结果为 passed。
- 首次 Linux CI 为 2577 通过、65 跳过、4 失败，Compose 冒烟通过。
  四个失败定位为迁移遗漏：waiting_operation 长度 17 超过旧 VARCHAR(16)。
  已将 ORM 与迁移统一扩为 20，并重跑；同时补充租约缓存竞争与执行中取消的回归。
- 租约检索在 SQL WHERE 中匹配 token，审批遵循 project→run 的加锁顺序。
  执行中取消不把已确认的外部成功误报为撤销，结果正文会清理，操作身份继续保留。

- 修复后的运行时/适配器/规则回归为 **51 通过、22 跳过**（Windows，30.46 秒）。

### 最终 CI 验收

代码提交 `29461f6` 的 [GitHub Actions](https://github.com/tangjujia9-jpg/Si-agent/actions/runs/38203484771)
已完成，两个任务均成功：

| 验收 | 结果 |
|---|---|
| Ruff | 全仓库检查通过 |
| Linux 全仓库确定性测试 | **2585 通过、65 跳过**，117.10 秒，无失败 |
| PostgreSQL + pgvector | 真实服务、隔离 schema、从零迁移及产品运行/审批/恢复用例通过 |
| React 测试 | **2 个文件、3 个测试通过** |
| React 生产构建 | 通过 |
| Docker Compose | 数据库、迁移、API、Worker、Web 构建与启动通过 |
| Compose HTTP 冒烟 | 导入、索引、引用、候选、版本、离线 Agent 回答和 SSE trace 通过 |

跳过项由测试条件决定，不能按通过计数。所有模型与 Jev 的 wire-format 验证采用
离线响应，OTLP 采用本地接收端；真实厂商质量、Langfuse 在线 UI 和 LLM judge 不在本轮结果内。
主路径的 PostgreSQL 与 Compose 已在线 CI 验证，不能用本机 SQLite 冒烟代替这一项。
