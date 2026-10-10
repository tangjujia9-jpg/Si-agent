# Si-agent 三周进展与源码导读

本文件记录前三周阶段。第四周当前能力和验收边界见[第四周报告](copilot-week4-release.md)。

截至本轮，Si-agent 已完成领域契约、可部署后端骨架和带引用的分层混合检索。
当前演示闭环是“创建项目 → 粘贴资料 → 后台导入与索引 → 查询 → 查看证据原文”。
完整聊天生成、自动记忆抽取与纠错、持久忘记、Langfuse 和 Eval Lab 尚待实现。

## 第一周：无第三方依赖的领域契约

“无第三方依赖”修饰的是领域契约模块，不是整个项目。
`dataclasses`、`typing.Protocol`、`datetime` 都来自 Python 标准库；这些模块
不导入 FastAPI、SQLAlchemy、LangChain 或模型 SDK，也不在导入时连接数据库。

| 契约 | 表达的业务含义 | 当前边界 |
|---|---|---|
| `RunContext` | 一次运行的用户、项目、会话、模型、预算与策略 | 结构已定义，旧 Loop 尚未统一采用 |
| `Budget` | token、迭代、工具次数与超时约束 | 约束字段已定义，不等于每个执行点都已拦截 |
| `ToolResult` | succeeded/failed/partial/unknown、操作 ID、重试和未完成步骤 | 统一协议已定义，旧工具迁移尚待进行 |
| `MemoryNode` / `MemoryHit` | 存什么、检索返回什么、怎样引用来源 | 第三周适配器已使用 |
| `MemoryPort` | 记忆的搜索、写入、读取、目录与忘记能力 | 第三周实现前四项，忘记等待 tombstone |
| `ProviderPort` | 模型请求、响应、流事件、能力与用量 | 多厂商运行时接线尚未完成 |

源码入口：`waku/domain/contracts.py`、`waku/memory/port.py`、
`waku/providers/contracts.py`。`__post_init__` 校验部分非法值；类型注解本身
不是完整运行时校验，HTTP 请求另由 Pydantic 处理。

作用是将业务表达与具体工具分开。例如 Context Compiler 接收 `MemoryHit`，
无需知道结果来自 PostgreSQL、Mem0 还是测试替身。未来更换模型 SDK 或存储
可以改适配器，同时保留业务契约和评测。第一周还保留 FTS5 基线，作为后续比较依据。

## 第二周：数据库、服务与执行可靠性

| 技术 | 做什么 | 为什么引入 |
|---|---|---|
| PostgreSQL | 真正持久化数据的数据库服务 | API/Worker 并发、项目关系、任务领取、全文与向量查询 |
| SQLAlchemy | Python 到 SQL 的映射、查询、连接和事务 | 组织模型、明确事务边界，减少路由散落裸 SQL |
| Alembic | 按版本升级数据库结构和数据 | 让旧数据库随部署升级，保留已有资料 |
| pgvector | PostgreSQL 内的向量类型与距离查询扩展 | 将向量与项目、状态、版本、时间过滤放在一起 |

原 SQLite 是单机文件数据库，简单、便携，FTS5 足以做关键词检索，也支持事务
和外键。它的限制主要是并发写入和本方案所需的 pgvector 路径，并非不能做持久化。
PostgreSQL 引入服务、迁移、部署与运维成本，换来多进程协作和统一检索存储。
SQLAlchemy 同时支持 SQLite，因此轻量测试不需要启动完整服务。

这些升级主要覆盖产品基础设施与存储边界。与 Harness 直接相关的是：

- API 使用幂等键、payload hash 和数据库唯一约束处理重复导入。
- Worker 使用行锁、`SKIP LOCKED`、租约与 token 防止重复领取和旧 Worker 提交。
- 失败状态与有限重试让任务可以恢复，并避免无限循环。
- 事务保证正文、索引更新和后续任务不会只提交一部分。

完整 Harness 还需要运行预算、工具校验、审批、外部动作 outbox、checkpoint
和统一 trace。新数据库只是其中一部分的底座。

## 现在打开控制台能做什么

标准 Compose 地址是 `http://127.0.0.1:8080`。

| 功能 | 已实现内容 |
|---|---|
| 项目管理 | 创建、切换项目，查询跟随当前项目范围 |
| 资料导入 | 输入逻辑相对路径并粘贴 Markdown、文本或代码 |
| 导入状态 | 查询 ingest 任务状态，成功后刷新目录 |
| Context Explorer | 展示目录与资料节点，使用 `si://` URI |
| 原文检查 | 查看 L0/L1/原文、revision 和导入事件 ID |
| Memory Search | 查询、L2 开关、片段、引用和打开原文 |
| 检索检查 | 展开编译上下文、阶段耗时、候选数量和降级提示 |

目前没有流式 Chat、模型选择设置、完整 Run Trace、版本差异、忘记按钮或 Eval Lab。
界面主要轮询 ingest，独立 index 任务可通过 API 查看；导入显示完成不能代表向量就绪。

## CI：怎样知道改动可以复现

CI 是 GitHub Actions 自动运行的检查。`.github/workflows/copilot.yml`
在 `main`、`codex/**` push 和 PR 时触发两类任务：

1. `contracts-api-storage`：安装锁定依赖，启动真实 pgvector PostgreSQL，
   运行 Ruff、契约/API/Worker/记忆/规则文档测试，再执行 React 测试、类型检查和构建。
   数据库用例使用随机独立 schema，并执行 Alembic 到 head。
2. `compose-smoke`：从 Dockerfile 构建 API/Worker/Web，等待迁移和健康检查，
   执行 smoke，验证项目、重复导入、索引、原文、引用检索与会话创建。
   失败输出日志，最后关闭容器并保留卷。

上游 `validate-skills.yml` 在 PR 和 main push 上运行全量离线测试、技能与配置检查；
`hosted-docker.yml` 检查上游 hosted；`release.yml` 用于版本 tag 发布。
新 Copilot CI 不调用付费模型，也不评价真实语义质量。上游全量检查与新链路的
验证结论分别记录，不能用某个绿色工作流替代全部验收。

## 第三周：Memory Port 如何落地读写

第一周描述接口，第三周的 `PostgresMemory` 把接口变成真实存储操作。

```python
memory = PostgresMemory(sessions, embedder)
memory.write(MemoryWrite(node, idempotency_key, evidence_ids=(event_id,)))
hits = memory.search(MemoryQuery(text="数据库决策", project_id=project_id))
source = memory.get(hits[0].uri)
```

写入保存节点、证据事件与幂等凭据，刷新关键词索引并排队 embedding；读取校验项目、
状态和有效时间。`list_children` 支持逻辑目录浏览。`forget` 仍未实现，避免在
缺少 tombstone 时让旧消息再次生成已删记忆。控制台导入与 Port 写入复用索引服务，
但导入采用独立任务流程，当前没有公开 HTTP 记忆写入接口。

## 关键词与向量在哪一步汇合

```text
React 提交查询
  → GET /api/memory/search
  → MemoryQuery → PostgresMemory.retrieve
  → 查询向量（如配置）
  → L0 目录：全文召回 + 向量召回 → RRF
  → L1 目录细化：全文召回 + 向量召回 → RRF
  → 叶节点：目录分支 + 全项目补充召回
  → 时间/状态/版本/范围过滤、融合、去重、top_k
  → 片段与引用 → compile_context → React
```

过滤条件实际参与各分支查询，避免先召回其他项目再在前端隐藏。
全文使用 `tsvector`、`to_tsquery` 和 `ts_rank_cd`，向量使用 pgvector 余弦距离。
RRF 按排名融合，避免直接相加单位不同的全文分与距离分。
当前是精确向量搜索，无 ANN 或模型 reranker。目录用于优先组织上下文；
全项目补充召回保护被截断目录摘要漏掉的相关正文。

## 当前哪里用了 RAG

RAG 包括检索和使用检索结果生成。新链路已具备资料准备、检索及增强上下文，
还没将上下文注入新模型请求。因此现在是 RAG 的 Retrieval 与上下文组装部分，
完整 Generation 尚未完成。控制台搜索不会让 LLM 写答案。

旧 CLI 已有记忆增强生成：`Session.build_system()` 调用
`Memory.gated_retrieve()`，gate 判定需要后检索 facts/episodes，把结果放进
`Relevant memory` 系统上下文，再由 Loop 调用模型。这属于已有的 memory RAG，
默认使用 SQLite/FTS5，也可以配置旧适配器；它没有自动切换到新的项目分层检索。
两条路径目前并行，后续必须在 Session/Context Compiler 接线，而非只新增向量表。

## 异步 embedding 在哪里

```text
导入请求（迅速返回 202）
  → ingest Worker 写原文、更新分层全文表示
  → 同一事务创建 index job
  → index Worker 在事务外计算文档向量
  → 续租、检查内容 revision、保存向量

搜索请求
  → 同步计算 query 向量 → 检索已保存的文档向量
```

异步化避免 HTTP 导入等待模型，并避免慢 embedding 长时间占用数据库事务。
正文更新会使旧向量失效，Worker 提交前检查快照，避免旧任务覆盖新内容。
embedding 服务失败时仍可走关键词路径。它不是前端 JavaScript 的 async，也不意味着
所有 embedding 都在后台；查询向量仍占用当前搜索请求时间。

默认本机 `none` 为纯关键词，Compose `hash-demo` 为哈希演示向量。
只有显式配置 `openai-compatible` embedding 服务才具有所选模型提供的语义能力，
真实效果仍需专门 benchmark。

## 带证据上下文从哪里来

证据来自用户提交的原文，索引不会凭空生成事实：

1. ingestion 保存原文到 `ContextNode.content`，将导入 job ID 放入 `source_event_ids`。
2. indexer 抽取 L0/L1，保留 L2 正文，写入 `ContextIndex.body` 并构建索引。
3. 检索得到节点，组装 `MemoryHit`，片段取自 body，evidence IDs 继承来源事件。
4. `compile_context` 添加 `[si://...]`、层级、事件 ID，再按预算截取片段。
5. 前端可以打开同一节点的原文核查来源。

例如文档写着“API 与 Worker 共用 PostgreSQL”，上下文会携带该文档 URI、导入
事件和对应片段，而非只返回一个脱离来源的摘要。当前没有精确字符偏移或独立
evidence 表；来源可追踪不等于来源正确，也不自动保证未来模型回答忠实。
预算目前按 UTF-8 字节保守估算，字段名称 `estimated_tokens` 不是精确 tokenizer。

## 本轮改名与文档整理

产品目录改为 `si://`，增加 `0003_si_namespace` 数据迁移，保留节点 ID、正文、
证据与向量。迁移同步更新父目录和导入结果根地址，碰撞时事务失败而非覆盖数据。
产品入口使用 Si-agent，移除首页上游品牌标志。Python `waku/` 包、原 CLI、上游
历史材料与许可证署名暂留兼容，全面改包名应单独安排兼容迁移。

领域设计、后端手册、记忆设计、第二/三周验收与后续路线图均改为中文。
未来开发验收继续使用中文，记录执行命令、通过/跳过、环境和未完成事项。
Langfuse 继续安排在运行时 Harness 接线阶段，统一关联 run、模型、工具、检索和
记忆写入；当前检索阶段数据不等于完整 Langfuse tracing。

本轮代码提交 `5ef7179` 已通过
[GitHub CI](https://github.com/tangjujia9-jpg/Si-agent/actions/runs/38021375535) 的
真实数据库与完整 Compose 检查。详细本地结果、跳过原因和验证限制见
[中文验收报告](copilot-week3-release.md#三周梳理与-si-改名验收)。

## 下一阶段建议

先完成第四周分块、候选记忆、历史版本与 tombstone，再接第五周运行时和 Langfuse。
这样生成可以使用稳定、可纠错的记忆接口，tracing 也能覆盖真实的写入与检索。
每个阶段继续用确定性边界测试和可复现 smoke 验证，再补真实模型质量 benchmark。

参阅[领域设计](context-copilot-design.md)、[后端手册](copilot-backend.md)、
[Memory v1](copilot-memory.md)、[第三周验收](copilot-week3-release.md)和[路线图](copilot-roadmap.md)。
