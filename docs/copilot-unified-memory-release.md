# Si-agent 统一记忆阶段验收报告

日期：2026-10-16。本轮在第六周完整界面之前实现统一记忆的第一阶段。
本轮完成新项目后端的检索复用、会话候选接线、留存统一、内容/表示版本分离和来源失效传播。
本轮没有将整个目标方案标记完成，也没有删除旧 CLI 或用户数据库。

## 本轮改变了什么

| 方面 | 原行为 | 本轮行为 |
|---|---|---|
| 检索路径 | 默认逐层目录路由并补召回 | 默认项目内 flat 混合召回，hierarchical 可选 |
| ReAct 频率 | 每次模型迭代都判门并可能检索 | 同一 Run 初始命中写入检查点，迭代与恢复复用 |
| 会话沉淀 | 新 Loop 只保存聊天，未自动提取 | 消息与 distill job 同事务写入，按会话水位增量生成候选 |
| 留存判断 | enrich 接 Jev，工具提议不接 | enrich、distill 和 propose_memory 共用留存判断 |
| 摘要更新 | 提高内容 revision，使证据过期 | 单独更新 representation_revision |
| 来源改动 | 已接受派生事实可能继续被召回 | 沿证据引用递归标记 needs_review 并移出默认索引 |
| 会话遗忘 | 尚无新会话来源节点 | session 来源忘记时脱敏原消息、相关运行与已知派生项 |
| 在线调度 | 索引可能先于会话执行 | 导入之后优先处理 Run/outbox，再处理后台工作 |

## 新的会话闭环

```text
用户发来消息
  → 保存带 sequence 的消息，同时登记延迟 distill 任务
  → 在线 Run 检索与回答
  → 后台根据 memory_cursor 和 through 读取增量
  → 生成用户消息来源节点和精确证据
  → 抽取、留存判断、保存待审候选
  → 人工接受
  → consolidate 校验证据和版本
  → 正式记忆进入默认检索
```

当前只从用户消息生成事实，助手自己的输出不会成为独立事实证据。
默认提取器只处理显式 Decision/决定 标记；普通自然语言需要配置模型抽取器。
当前模型逐条处理用户消息，未实现跨消息指代解析、工具经验蒸馏或自动情景总结。
后台任务延迟约 30 秒可执行；持续繁忙时会排队，并非保证 30 秒完成。
低流量会话也有持久化任务，不必凑够固定轮数。
候选持久化后推进进度，重试或重叠批次不能重复生成相同来源候选。

## 数据库与升级

`0006_unified_memory` 增加线程 event_sequence/memory_cursor、消息 sequence、
节点 representation_revision/needs_review 与索引 representation_revision。
迁移为既有 PostgreSQL 消息按会话编号，内容、URI、证据和旧版本保持原样。
历史消息不会自动触发全库重抽，用户可以对指定会话调用 distill。

升级时先停止旧 API 和 Worker，然后执行：

```powershell
cd C:\code\agent-study\waku-agent
docker compose -f infra/compose.yaml stop api worker
docker compose -f infra/compose.yaml up -d --build
```

Compose 会执行 Alembic。使用本机 Python 时先运行 `uv run --no-sync alembic upgrade head`，
再启动 API 与 Worker。旧进程不应与新版本同时写入。
迁移降级会拒绝丢弃已经处理的蒸馏水位、任务或重审状态，不能用降级命令绕过数据导出。

## 手动验收

打开前端 http://127.0.0.1:8080 和 API 文档 http://127.0.0.1:8000/docs。
React 页面仍提供项目、资料导入、目录、原文和检索，候选审核与聊天需要使用 API。

1. 创建项目与会话，提交消息 `决定[storage]: 项目使用 PostgreSQL 和 pgvector。`。
2. 调用 `POST /api/threads/{id}/memory/distill`，查看返回的 job 状态。
3. 调用 `GET /api/projects/{id}/memory/candidates`，确认候选为 pending。
4. 调用 `POST /api/memory/candidates/{id}/apply`，等待 consolidate 完成。
5. 搜索 PostgreSQL，确认正式记忆、URI 与来源证据可读取。
6. 再发一条不同决策并执行 distill，确认只新增对应候选。
7. 导入文档并基于它接受记忆，再修改原文，确认旧派生事实 needs_review=true 且不再默认召回。
8. 比较同一搜索的 strategy=flat 与 strategy=hierarchical，查看 stages 的目录步骤差异。

直接检查聊天来源时，可在搜索 API 指定 kinds=session；默认搜索不混入所有用户聊天。
`scripts/smoke_copilot.py` 已扩展到会话标记、蒸馏、待审、接受及正式节点读取。
脚本只创建新演示项目，不删除已有资料。

## 验证结果

本机相关后端与规则文档回归已通过 106 项，77 项因未配置真实 PostgreSQL 或可选条件而跳过。
Ruff 已通过。新增用例验证检索复用、摘要证据稳定、依赖失效、增量与重试、
助手事实隔离、遗忘、租约丢失和统一留存策略。
React 的 3 项测试、TypeScript 检查和 Vite 生产构建均通过。
测试故意关闭来源失效标记时，来源更新用例失败，恢复实现后通过。
完整验证结果会在本轮最后核对后更新。

本机 Docker 引擎当前不可用，因此尚未在本机确认真实 PostgreSQL 迁移和 Compose 闭环。
离线标记和脚本模型测试不等于真实 Jev、Embedding、生成模型或 Langfuse 质量验收。

## 后续仍需完成

- 旧 CLI 接统一 Runtime 与显式 SQLite 历史导入；本轮保持兼容且不双写。
- 用户自然语言明确修正、忘记的完整工具协议与即时工作上下文覆盖。
- 必须生效的约束层、个人范围与技能启用管理。
- 跨消息指代改写、事实键对齐、语义冲突建议和自动重审任务。
- 会话/工具事件统一来源协议；当前 session 是原消息引用加不可变快照。
- 多个独立证据的支持判断、同文档多片段选择与 reranker 实验。
- Chat、候选审批、版本对比、Run Trace 与正式质量 benchmark。

本轮的失效策略优先阻止旧结论被当作当前事实。系统不会自动决定哪项矛盾结论正确，
也不会在缓存失效后自动恢复运行；当前会安全停止并保留可检查状态。
