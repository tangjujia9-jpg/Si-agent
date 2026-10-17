# Si-agent 第五周运行时设计

本轮按已确认的改造方案实现项目运行时，旧 CLI 保持独立。新运行时拥有 Memory Port、
Provider Port、工具契约和观测事件，LangGraph 与 Langfuse 作为可选适配层。

## 执行与恢复

API 将用户消息与运行任务一起提交；Worker 驱动运行。每次模型响应、工具计划和工具结果
形成可序列化检查点。运行保存预算、截止时间、租约及任务状态。模型请求可以重试，
已知工具结果不能重复执行；无法判断外部动作是否成功时返回 unknown，并要求核查。

工具使用 Pydantic 校验入参，按运行允许集合暴露。写工具生成操作记录，经审批后进入
事务内 outbox；外部调用在事务外执行。仅声明支持幂等的动作可在中断后安全重试。
审批表示用户允许执行某次具体操作，模型不能自行批准。

## 上下文与模型

运行在首次需要模型输入时通过检索门，再召回当前项目有效证据。
同一 Run 将初始命中保存到检查点，后续模型迭代和审批恢复复用命中；明确搜索工具仍可追加查询。
缓存证据在每次外部调用前检查来源，失效时安全停止，不继续使用旧事实。Context Compiler 将引用和上下文视为
不可信资料，限制上下文预算。所有模型使用可序列化消息，Provider 转换各自协议，
不传递 SDK 专用对象。首版提供 OpenAI、Anthropic、OpenAI-compatible 和离线 demo。

## Jev 决策边界

TypeSafe Jev 返回分类、概率和置信度，不能负责生成摘要、查询改写或事实文本。
统一阶段已接入检索门、enrich/会话候选与 propose_memory 工具的长期留存筛选；相关性评分协议保留，尚未启用重排。低置信度或服务错误时
检索门保守召回，候选保持待审。Jev 不绕过项目隔离、证据校验、版本冲突和删除屏障。
缺少密钥时默认本地规则，不触发外部决策服务。真实质量和阈值需要第七周数据集验证。

## 可观测性

数据库事件是产品记录，Langfuse 是可选导出。span 包围真实模型、检索与工具操作，
保留父子关系、usage、错误类型与操作身份。默认不采集 prompt、原文和工具 payload；
进入数据库事件和导出前先脱敏。导出队列有界，故障不阻塞业务，退出时有界 flush。
辅助调用的预算和 usage 必须与主模型分开标记。

验收结果与边界见[第五周报告](copilot-week5-release.md)。

## 配置与调用

核心运行时只需 `copilot` extra。Jev、Langfuse 和 LangGraph 都可以分别安装：

```powershell
uv sync --frozen --extra dev --extra copilot --extra copilot-jev --extra copilot-observability --extra copilot-workflow
```

启动方法见[后端手册](copilot-backend.md)。完整配置字段示例在
`infra/copilot.env.example`，它独立于旧 CLI 自动生成的 `.env.example`。
Compose 可用 `docker compose --env-file infra/copilot.env -f infra/compose.yaml up -d --build`。
不要把填写密钥后的配置提交 Git。

| 配置 | 作用 |
|---|---|
| `SI_OPENAI_API_KEY/MODEL/BASE_URL` | OpenAI 模型与后端凭据，可选自定义地址 |
| `SI_ANTHROPIC_API_KEY/MODEL/BASE_URL` | Anthropic 模型与后端凭据 |
| `SI_COMPATIBLE_BASE_URL/MODEL/API_KEY` | Ollama、vLLM 或其他兼容接口；本地可不填 key |
| `SI_{OPENAI,ANTHROPIC,COMPATIBLE}_CONTEXT_WINDOW` | 显式配置上下文窗口，未知按 8192 控制 |
| `SI_{OPENAI,ANTHROPIC,COMPATIBLE}_TOOL_CALLING` | 无工具能力设为 false，并在运行中传空 tools |
| `SI_DECISION_BACKEND=local/jev` | 默认 local，不调用外部决策服务 |
| `SI_JEV_API_KEY`、`SI_JEV_MODEL` | 显式启用决策服务时使用，默认模型 jev |
| `SI_LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY` | 导出到已有 Langfuse 实例；HOST 留空则停用 |

Jev 的调用 timeout 为 5 秒，SDK 重试关闭。Langfuse 使用 HTTP OTLP 路径
`/api/public/otel/v1/traces` 与 public:secret Basic 鉴权，外部地址要求 HTTPS。
HTTP 只接受精确的本机或 Compose Langfuse 主机名。首版仅采集 metadata，尚无原文采集开关。

API 消息示例：

```json
{
  "content": "项目为什么选择 pgvector？请引用资料。",
  "idempotency_key": "turn-001",
  "provider": "openai-compatible",
  "model": "your-model",
  "tools": ["search_memory", "read_memory", "propose_memory"],
  "max_iterations": 8,
  "max_tokens": 16000,
  "max_tool_calls": 10,
  "timeout_seconds": 300
}
```

向 `POST /api/threads/{thread_id}/messages` 提交后拿到 run ID，再订阅
`GET /api/runs/{id}/events/stream?after=0`。重连可用已收到的序号作为 after。
SSE 包含 run_start、memory_search、llm_start/end、tool_start/end、approval_required、
span、run_pause/end 和最终 snapshot；目前没有聊天 token delta。
浏览器断线自动使用 Last-Event-ID 续接，手动客户端也可传 after；审批暂停时连接继续发送快照。
页面刷新从持久化消息与运行记录恢复。活动运行截止时间仍包含等待用户审批的时间。

`GET /api/runs/{id}` 可检查工具入参与结果；
`POST /api/operations/{id}/approval` 接受 `{"approved": true}` 或 false；
`POST /api/runs/{id}/cancel` 请求取消。unknown 无自动重试或强制恢复 API，先人工核查目标资源。

## 关键源码

| 文件 | 职责 |
|---|---|
| `waku/runtime/runner.py` | 自主 Loop、检查点、预算、恢复、审批/outbox 驱动 |
| `waku/tools/executor.py`、`memory_actions.py` | 严格 schema、动态工具、受控记忆变更与结构化结果 |
| `waku/providers/adapters.py`、`catalog.py` | 模型协议转换、流式适配、后端凭据与能力目录 |
| `waku/runtime/decisions.py` | 本地规则、Jev、置信度与错误降级 |
| `waku/runtime/telemetry.py` | 真实调用 span、脱敏、OTLP 与有界导出 |
| `waku/server/runs.py` | 会话运行、查询、SSE、审批和取消 API |
| `waku/workflows/langgraph.py` | 可选 interrupt/resume workflow，调用者注入 checkpointer |

参考资料：[Jev/System One](https://typesafe.ai/blog/introducing-system-one-models-and-jev)、
[TypeSafe SDK](https://github.com/typesafe-ai/typesafe-sdk-python)。真实决策质量与阈值需要独立数据集，
SDK wire-format 测试不能替代质量评测。

统一阶段的会话蒸馏、表示版本与默认检索变更见[统一记忆验收报告](copilot-unified-memory-release.md)。
