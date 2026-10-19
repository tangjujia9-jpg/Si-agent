# Si-agent 记忆：来源、候选、索引和读取

Si-agent 的项目记忆使用 PostgreSQL 与 pgvector。系统将原始资料、会话来源和已接受记忆
组织为 `si://` 节点，通过关键词与向量检索返回引用。API、Worker 和工具使用 MemoryService；
旧 Waku CLI 的 SQLite 链路仍独立，系统不会自动迁移或删除原数据库。

## 存储与接口

`context_nodes` 保存 URI、父目录、类别、正文、L0 abstract、L1 overview 和状态。
`context_indexes` 保存各层表示、正文分块、全文索引和向量。
`memory_candidates` 保存尚未接受的建议，`memory_evidence` 保存原文精确区间，
`memory_versions` 保存内容历史，`forget_tombstones` 阻止已忘记内容从旧任务恢复。

虚拟目录是数据库视图，系统没有挂载磁盘文件系统，也没有运行 OpenViking 数据库。
短正式记忆的摘要由确定性规则立即产生；资源可另外开启模型摘要。

MemoryService 统一项目读取、候选提交、人工接受和忘记。PostgresMemory 继续实现底层
MemoryPort 的读取和内部写入。Agent 只能通过受控工具提议，不能直接调用内部 repository 写入。

## 文档与会话写入

文档导入先保存完整正文与初始摘要，构建全文表示，并提交 index 与 enrich 任务。
enrich 可以生成模型摘要并抽取候选；抽取结果经过证据检查和可选留存分类后保持待审。
人工 candidate API 提交的具体变更会直接排队；自动抽取与 propose_memory 不自动接受。

新项目消息使用会话内递增 sequence。消息与延迟约 30 秒可执行的 distill 任务在同一事务登记。
Worker 根据该会话 memory_cursor 与任务 through 水位选择尚未处理消息，单批最多 100 条。
多批处理重新排队同一任务，进度只在候选与来源提交后推进，不等待人工审核。
`POST /api/threads/{id}/memory/distill` 可以立即处理当前水位。

本阶段只从用户消息产生事实，助手输出不作为新事实的独立证据。
默认离线提取识别 `Decision[key]: text` 或 `决定[key]: text`；配置
`SI_EXTRACTION_BACKEND=openai-compatible` 后可以处理普通正文。
当前模型抽取仍以每条用户消息为目标，可附带最近 4 条用户陈述和最多 8 条有效记忆辅助解析指代。
引用必须来自当前消息；背景用户陈述和相关记忆的证据作为依赖保存并在提交时复核。
系统排除助手陈述，含糊指代应放弃抽取；该策略尚未通过真实模型的质量基准。
系统尚未将工具轨迹提炼成经验。

消息来源会生成不可变 session 节点，引用原消息 ID。它是用于证据读取的受控快照，
原聊天记录仍然存在；forget 会同时脱敏关联消息和已知运行检查点。
session 默认不参与事实召回，只有显式 kinds=session 才检索聊天来源正文。
同一事实键对应的有效节点会形成带 expected_revision 的 UPDATE 建议；模型也可指定供给上下文中的目标 URI。
同文去重、待审、版本冲突与忘记屏障继续生效。候选引用来源证据，人工接受后才形成 memories、decisions 或 skills 等目录中的正式节点。

## Jev 留存与审批

文档抽取、用户消息抽取和 Agent 提议使用同一类留存策略。
显式人工提交的 ADD、UPDATE、SUPERSEDES、RETRACT、SKIP 直接进入确定性校验，不调用留存分类。
启用 Jev 时，高置信度否定使自动候选跳过，肯定或不确定结果仍然待审。
缺少 Jev 时默认本地待审策略。检索门失败保守召回，留存失败保留候选。
Jev 不负责证明事实正确，也不能替代证据、范围、版本或删除校验。

## 内容版本与依赖

`revision` 现在表示节点内容与业务状态版本。`representation_revision` 表示摘要和目录表示版本。
重建摘要不再增加内容 revision，因此旧的精确原文证据与待审候选仍然有效。
异步索引提交同时核对两种版本、正文和租约，旧向量不能覆盖新表示。

系统通过正式记忆 source_event_ids 中引用的 evidence IDs 跟踪派生关系。
来源被修改、替代或撤回后，系统递归将已知派生节点标记 needs_review，移除默认索引。
这是一种保守失效策略；系统尚未自动判断多个证据能否独立支持同一事实。
用户用新证据提交 UPDATE 后，可以使该节点恢复有效。
新一轮模型请求不会携带证据已失效的旧助手回答；历史消息仍保留以供审计。
系统尚未实现语义矛盾推理、跨 URI 自动合并或自动选择更可信来源。

## 检索与上下文

默认策略为项目范围内的 flat 混合召回，不强制先进行目录导航。
`GET /api/memory/search` 支持 strategy=flat 或 hierarchical；后者保留目录路由与全局补召回作对比。
默认 kinds 为 resource、semantic、episodic、procedural，可显式指定 session。
用户在前端主动搜索时直接执行检索；Agent 的自动召回先通过检索门。

检索依次验证项目归属、生成一次查询向量、过滤有效内容与版本，然后在所选层级召回。
关键词使用 PostgreSQL tsvector/to_tsquery 与 ts_rank_cd，向量使用 pgvector 余弦距离，
两路结果用 RRF 融合。默认仍按节点选择最佳片段，尚无独立 reranker 或多片段多样性选择。
层级表示有 L0 摘要、L1 概览和 L2 正文分块；正文按约 2400 字符、200 字符重叠切分。

Run 把首次检索的 MemoryHit 保存进检查点，后续模型迭代与审批恢复复用它们。
明确 search_memory 工具仍可追加查询。每次外部派发前检查来源内容版本、重审与删除状态；
发现失效时停止运行，当前尚未自动重新规划恢复。
本阶段尚未接入生成式查询改写、完整约束常驻层或跨项目个人范围召回。

MemoryHit 包含 URI、内容 revision、片段、层级、证据 ID 和有效时间。
L2 证据指向原文区间；L0/L1 的证据用于追溯原文，不表示摘要自身是精确原文。
Context Compiler 保留引用头并按 UTF-8 字节保守限制预算，再由 Runtime 连同历史、工具契约与系统规则
组成 Provider 请求。React 已支持对话、引用打开、候选审核、版本对比与运行轨迹。
用户可以从检查台提交有独立新陈述证据的修正、撤回事实或预览并忘记来源。

## 模型配置与验证

| 配置 | 行为 |
|---|---|
| SI_EMBEDDING_BACKEND=none | 仅关键词，本机默认 |
| SI_EMBEDDING_BACKEND=hash-demo | 离线演示向量，Compose 默认，不提供语义改写召回 |
| SI_EMBEDDING_BACKEND=openai-compatible | 需要 BASE_URL、MODEL，可选 API_KEY，维度通过 SI_EMBEDDING_DIMENSIONS 配置，默认 1536 |
| SI_EMBEDDING_BATCH_SIZE | 每批 1–16 条文本，本轮限额模型配置为 4 |
| SI_SUMMARY_BACKEND=extractive/openai-compatible | 规则摘要或可选模型摘要 |
| SI_EXTRACTION_BACKEND=markers/openai-compatible | 决策标记或可选模型抽取 |
| SI_DECISION_BACKEND=local/jev | 本地策略或可选 Jev |

API 与 Worker 必须使用相同 Embedding 配置。查询向量在搜索时计算，资料向量在后台建立。
换 Embedding 模型、服务或请求维度后需要 reindex，查询不会混用不同模型向量。
迁移 0007 支持不同维度共存，距离运算按模型与维度隔离。当前 Qwen 接口实测 1024 维，不能补零伪装成 1536 维。
当前向量采用精确检索，尚未建立 HNSW/IVFFlat 路径。

hy3/Qwen 的实际配置、限额与初步基线见[7A 验收报告](copilot-week7a-release.md)。本轮禁用 Jev。

离线测试验证程序边界，不证明真实模型质量。实际完成范围、验证结果和手动操作步骤见
[统一阶段验收报告](copilot-unified-memory-release.md)，长期目标见[统一记忆设计](si-unified-memory-design.md)。
