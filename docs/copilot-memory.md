# Si-agent Memory v1：索引、检索和证据

Memory v1 将用户导入的项目资料保存为 `si://` 节点，建立 L0/L1/L2 表示，
通过关键词与向量检索返回可检查的证据上下文。当前版本完成检索和上下文组装，
尚未把它接入旧 Agent Loop 生成回答。

## Memory Port 与适配器

`waku/memory/port.py` 使用标准库 `Protocol` 描述 `search`、`write`、`get`、
`list_children` 和 `forget`。业务方依赖这些能力，具体存储由适配器实现。
`waku/memory/postgres.py` 的 `PostgresMemory` 已实现五项；第四周 forget
写入 tombstone，并脱敏原文、历史、证据和已知派生记忆。

`write(MemoryWrite)` 校验用户、项目与规范 URI，补齐父目录，在一个事务中
保存正文、来源事件、revision、幂等凭据，刷新全文索引并排队向量任务。
相同请求重试返回同一凭据，幂等键对应不同请求会报错。调用方可以显式指定
`supersedes_id` 替代同项目旧节点；系统尚未自动推理冲突。
这个接口是 Python 内部能力，控制台导入走独立 ingestion 路径。第四周的 HTTP
candidate 接口支持异步更新、替代和撤回；原文更新仍通过重新导入。

## 虚拟目录与层级表示

```text
si://users/default/projects/{project_id}/
  resources/docs/design.md
  decisions/database.md
```

URI 是数据库中的逻辑地址，不代表宿主机的实际文件。导入路径组织到 resources；
决策路径可通过 Memory Port 写入。personal、skills 和跨项目授权仍待实现。

| 层级 | 叶节点表示 | 目录表示 |
|---|---|---|
| L0 abstract | 首个非空行，最多 240 字符 | 聚合子节点 abstract，最多 600 字符 |
| L1 overview | 正文前 1200 字符 | 聚合子节点 overview，最多 6000 字符 |
| L2 detail | 2400 字符以内的原文分块，200 字符重叠 | 不为目录创建 L2 |

初始 L0/L1 由规则抽取，后台 enrich 可以用分块抽取或显式配置的模型生成摘要。
`context_nodes` 保存完整原文，`context_indexes` 保存各层级/分块的 body、position、
原文区间、全文索引、embedding、embedding_model 与 revision。
L2 每块独立计算向量，改善第三周长文尾部语义覆盖；L1 输入仍有界。

## 写入与异步 embedding

```text
POST ingest → queued ingest job → Worker 写原文与层级全文索引
  → 同一事务排队 index job → HTTP 可查询原文与关键词
  → Worker 读取索引快照 → 事务外批量 embedding
  → 校验 lease token、revision、body → 保存向量 → index succeeded
```

导入成功不等于向量就绪。导入结果中的 `index_job_id` 指向向量任务，
可通过任务接口查看。界面当前主要跟踪 ingest；检索响应另有索引覆盖和降级提示。
第四周同时返回 `enrich_job_id`，后台执行摘要与候选抽取，并按需创建后续索引任务。
索引 Worker 分批计算、续租、有限重试，并跳过在计算期间被修改的快照，
防止旧向量覆盖新内容。embedding 失败时关键词仍可检索。

异步指资料向量化在后台执行。查询时需要查询向量，因此 query embedding
仍在搜索请求内同步完成；配置远程服务后，此步骤会增加查询耗时。

## embedding 配置

| `SI_EMBEDDING_BACKEND` | 行为 |
|---|---|
| `none` | 不调用 embedding，执行关键词检索；本机默认 |
| `hash-demo` | 离线、确定性 1536 维哈希演示向量；Compose 默认，不具备语义理解 |
| `openai-compatible` | 后端请求 `{base_url}/embeddings`；需要显式配置模型与端点 |

真实服务还需要 `SI_EMBEDDING_BASE_URL`、`SI_EMBEDDING_MODEL`，按需设置
`SI_EMBEDDING_API_KEY`。这是兼容协议，不限定模型厂商。当前表结构固定 1536 维，
服务必须返回此维数；代码拒绝 NaN、零向量与数量不符。
一次最多 16 条、超时 20 秒。API 和 Worker 必须使用相同配置，模型标识包含端点
指纹与模型名，检索不会混用不同模型向量。切换模型后调用 reindex 并等待任务完成。

## 查询时的混合检索

入口是 `GET /api/memory/search`，经 `MemoryQuery` 调用
`PostgresMemory.retrieve()`。混合检索就在这个读取阶段执行，而非导入阶段：

1. 校验项目归属，再按配置计算 query embedding。
2. 排除跨项目、非 active、过期或 revision 不符的索引。
3. 对 L0 目录分别进行关键词与向量召回，RRF 融合后保留少量候选。
4. 细化 L1 目录，再检索所需层级的叶节点。
5. 额外执行项目范围叶节点召回，避免截断的目录摘要漏掉正文中的相关事实。
6. 融合排名、去重与 top_k，提取片段，再按上下文预算组装。

PostgreSQL 关键词路径是 `tsvector @@ to_tsquery` 和 `ts_rank_cd`，支持 GIN
索引；它不等同于 BM25。分词包含英文单词与中文 unigram/bigram，尚非完整中文 NLP。
向量路径是 pgvector 余弦距离，当前采用精确搜索而非 HNSW/IVFFlat。
每一支贡献 `1/(60+rank)` 的 RRF 分数；目录命中给予少量优先级。
系统尚未添加 cross-encoder reranker 或新的 LLM Retrieval Gate。
SQLite 适配使用 Python 的关键词计数与余弦计算，只验证流程和边界，不代表 pgvector 性能。

## 证据从哪里来

```text
用户导入原文 → ContextNode.content / source_event_ids=[ingest_job_id]
  → ContextIndex.body → 检索命中 → MemoryHit.snippet / evidence_ids
  → compile_context → [si://...] + 层级 + 来源事件 + 正文片段
```

`MemoryHit` 包含节点 ID、URI、层级、片段、分数、来源类型、项目、有效时间和
evidence IDs。关键词片段从匹配附近截取；纯向量命中可能取原文开头。
React 可以根据节点 ID 打开完整原文。L2 证据 ID 指向精确 quote、来源 revision
与原始导入事件；L0/L1 仍提供来源事件，不假装摘要是原文区间。
证据来源不证明资料正确，也不保证片段一定完整支持结论。
第四周已提供独立 evidence、精确 L2 字符区间与历史版本。自动抽取为待确认候选，
规则默认识别明确决策标记，也可选择模型抽取；不会直接将模型建议写成事实。

示例上下文：

```text
[si://users/default/projects/p/resources/docs/design.md] (l2, evidence: ingest-job-id)
使用 PostgreSQL 保存项目资料，通过 pgvector 和全文检索返回引用证据。
```

`waku/memory/context.py` 保留完整引用头，片段截断不破坏 UTF-8。
字段虽然叫 `estimated_tokens`，当前实现实际按 UTF-8 字节保守计数，并非模型 tokenizer。
生成接入后仍需计算 system prompt、历史消息、工具 schema 和输出余量的整体预算。

## 当前 RAG 的完成范围

导入、表示、索引、检索、上下文组装构成 RAG 的准备与 Retrieval 部分。
下一步应在 Session/Context Compiler 中调用这个 Port，把带引用上下文注入模型请求，
再通过生成与 groundedness 评测验证回答。当前 React 查询不会调用生成模型，
旧 CLI 的 `Session.build_system()` 已调用 `Memory.gated_retrieve()`，将
facts/episodes 加入 `Relevant memory` 后交给旧 Loop 生成回答，属于既有的
记忆增强生成。它仍走原记忆链路，尚未使用新的项目 Port 与 `si://` 证据。
因此当前新控制台的检索演示尚不能称为完整的项目 RAG Agent。

## 验证与后续工作

确定性测试位于 `evals/deterministic/test_copilot_memory.py`，覆盖范围隔离、有效期、
幂等写入、显式替代、预算、中文尾部召回、向量单独召回、模型隔离、租约和快照变化。
语义 fixture 验证程序分支，hash-demo 验证可复现链路，都不能证明真实 embedding 的检索质量。
真实 PostgreSQL/pgvector 和 Compose 由 Copilot CI 验证。

第四周实现分块、候选、版本、显式冲突和 tombstone，详见[第四周报告](copilot-week4-release.md)。
第五周接 Runtime 与 Langfuse，详见[开发路线图](copilot-roadmap.md)。
