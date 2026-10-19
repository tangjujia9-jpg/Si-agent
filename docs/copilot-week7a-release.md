# 第七周 7A：真实模型接入与检索基线

2026-10-19，Si-agent 已接通 hy3 和真实 Embedding，并完成导入、问答、记忆修正及跨会话引用的小样本验收。检索运行器已建立首份可重复基线。独立人工标注、PostgreSQL 质量基线、留存评分和批量答案评测仍待完成。

## 模型配置与能力验收

| 用途 | 当前配置 | 已验证范围 |
|---|---|---|
| 主模型 | OpenAI-compatible / hy3，经本机 18790 SSH 隧道 | 文本回答、结构化工具调用及结果回传、Provider 流式协议 |
| 多模态 | 同一 hy3 接口 | 32×32 合成蓝色 PNG 的颜色识别；前端尚无图片上传 |
| 候选抽取 | hy3 | 导入资料抽出三条有原文证据的事实/规范，经审核写入 |
| 摘要 | 产品采用 extractive | hy3 的 JSON 摘要另做小样本验证；基线固定摘要以排除抽取差异 |
| Embedding | qwen3.7-text-embedding-flash | 实际返回 1024 维，按 4 条文本批处理 |
| 检索门与留存策略 | local | 本轮按用户要求禁用 Jev |

模型窗口配置为 32768，这是应用侧上限，不代表已验证服务的真实最大窗口。
Provider 支持流式协议也不代表聊天页面已逐 token 输出；当前页面仍展示 SSE 运行进度与最终回答。
密钥只保存在忽略的 `infra/copilot.env`，前端从 API 获取公开模型配置。

## 1024 维兼容与数据库迁移

Embedding 服务在请求 1536 维时仍返回了 1024 维。原实现固定校验 1536 维，因此不能直接切换该模型。
现在 `SI_EMBEDDING_DIMENSIONS=1024` 明确指定预期维度，客户端严格校验实际结果，不补零也不截断。
未配置维度时继续采用原来的 1536 维兼容路径。

迁移 `0007_embedding_dimensions` 将两张索引相关表的 `vector(1536)` 放宽为 `vector`，保留已有向量。
模型身份包含服务地址摘要、模型名、维度及是否显式请求投影，不把旧演示向量当成新模型索引。
PostgreSQL 在距离计算的 CASE 操作数中同时校验模型和维度，防止 SQL 优化器先计算不兼容向量。
SQLite 的对应适配器跳过长度不同的向量。当前仍采用精确向量搜索，尚无 HNSW/IVFFlat 索引。

升级前备份数据库，然后执行 `alembic upgrade head`。API 和 Worker 必须同时更新配置并重启；旧资料需要重新索引。
迁移本身不调用模型、不清空数据。若已有非 1536 维向量，降级转换会在事务内失败，不能通过删除向量强行降级。

## 本机启动与查看

本次开发保留原演示数据库，并通过 SQLite backup API 另建 `.artifacts/dev/si-real-models.db`。
已有四个演示项目被保留，真实模型验收使用独立项目“真实模型验收-20261019”。
这个数据库用于本机验收；产品主路径仍为 PostgreSQL，不能把本机成绩写成 pgvector 实测。

首先保持用户已经建立的 SSH 隧道运行：

```powershell
ssh -N -L 18790:127.0.0.1:18790 aliyun-tang
```

`infra/real-models.env.example` 是无密钥模板。将实际配置保存在忽略的 `infra/copilot.env`，并配置已有的 `SI_DATABASE_URL`。
启动脚本不创建或迁移数据库；首次完整部署请按[后端手册](copilot-backend.md)运行 Compose。
已经安装依赖时，在仓库根目录的三个独立终端执行：

```powershell
.\.venv\Scripts\python.exe scripts/run_copilot.py api --config infra/copilot.env
```

```powershell
.\.venv\Scripts\python.exe scripts/run_copilot.py worker --config infra/copilot.env
```

```powershell
.\.venv\Scripts\python.exe scripts/run_copilot.py web
```

第三个命令需要 Node.js 在 PATH 中，且 `apps/web/node_modules` 已安装。
打开 `http://127.0.0.1:5186`，API 文档位于 `http://127.0.0.1:8016/docs`。
在聊天模型选择器中选择 `openai-compatible / hy3`；默认演示模型依然存在，选择它不会调用真实主模型。
在验收项目中查看资料树、候选审核、记忆版本、会话和 Run Trace。
重启时先用 Ctrl+C 停止对应终端，避免启动两个 API 或并发消费同一个 SQLite 开发库。

Docker 中的 `127.0.0.1` 指向容器自身，不能直接使用宿主机回环隧道。
Windows Docker Desktop 可以尝试 `host.docker.internal:18790`，但宿主机隧道监听和防火墙也必须允许连接；本轮没有扩大 SSH 监听范围。
Compose 读取配置时使用 `docker compose --env-file infra/copilot.env -f infra/compose.yaml up -d --build`。
模型密钥只传给后端服务；Web 启动器剔除 SI_/VITE_ 配置后仅注入 API 代理地址。

## 端到端验收

导入资料声明主数据库采用 PostgreSQL/pgvector、写工具需要人工审批、项目资料必须隔离。
后台完成导入、模型抽取、候选审核写入及索引，hy3 首轮回答正确引用资料 URI。
之后通过记忆修正接口增加“每天凌晨 02:00 进行加密备份”，审核后内容从 revision 1 更新到 2。
新会话在没有前一会话历史的情况下回答 PostgreSQL/pgvector 和 02:00，并引用 revision 2 对应的记忆 URI。
版本、证据和实际运行摘要保存在[验收数据](../evals/reports/real_model_acceptance_v1.json)。

首次跨会话尝试使用 24000 累计预算，模型先调用搜索、再读取记忆，最终被 `context_budget` 拦截。
该运行记录了三次模型响应、2557 个服务报告 token 和 21007 个保守预留 token，未生成最终答案。
之后在新会话显式设置 64000 累计预算完成复测。这个数值不改变单次 32768 的窗口限制。
最终成功样本只用一轮回答，因此不能据此声称工具循环的效率问题已经解决。
后续应优化 UTF-8 保守估算、重复工具结果与上下文压缩，并同时记录预算预留和服务实际 usage。

第二次成功问答后的自动会话蒸馏遇到模型服务 HTTP 503，三次重试后进入 failed。
回答与显式记忆修正已经落库，不受该任务失败影响；该会话的后续自动整理尚未完成。
当前没有人工重试失败任务的完整入口，后续需要补充服务退避、错误分类与可审计的恢复操作。

## 固定检索基线

数据集 `si-project-memory-v1` 包含 24 篇合成资料、48 条中文检索问题和 16 条留存参考样本，共 64 条。
数据集由 Agent 编写并提供精确参考原文，尚未经过独立人工复核，因此未达到原计划的“60 条人工标注”验收要求。
其中 39 条检索问题有正确来源，9 条要求无答案。数据已区分 calibration/test；本次数字为全量探索基线，不能当作未见测试集成绩。
留存 16 条只建立标签，本轮没有评分，也没有启用 Jev 或 LLM judge。

两种检索使用相同语料、正确记忆、摘要、查询、有效时间、top-k=5 和上下文预算 3000。
运行器调用实际 Memory Port、索引和有效性过滤，直接写入固定正确记忆，单独隔离模型抽取质量。
SQLite 的 lexical 分支使用 Copilot 关键词适配逻辑；它不是原 Waku 的 FTS5 实现，也不是 PostgreSQL 全文检索。

| 指标 | SQLite lexical | SQLite hybrid + Qwen 1024 |
|---|---:|---:|
| Recall@5 | 0.871795 | 0.923077 |
| MRR | 0.746581 | 0.822650 |
| nDCG@5 | 0.777981 | 0.848342 |
| 来源证据完整性 | 1.0 | 1.0 |
| 项目泄漏/禁止来源/过期/不可用命中查询 | 0 / 0 / 0 / 0 | 0 / 0 / 0 / 0 |
| 无答案问题仍返回邻近资料 | 9 / 9 | 9 / 9 |
| 平均查询耗时 | 28.807 ms | 46.666 ms |

上述召回指标只对 39 条正例求均值。证据完整性检查原文版本与字符区间，不能当作生成答案的引用准确率。
平均耗时来自 SQLite 与预热向量缓存，不能代表网络冷请求、PostgreSQL 性能或生产 p95。
76/76 个有效表示完成索引。无答案问题仍返回邻近内容，说明后续需要评估拒答与相关性阈值。
逐问题结果和冻结配置见[基线 JSON](../evals/reports/project_memory_v1.sqlite.json)。

向量预算器在网络请求前持久化预留额度，失败也计入尝试，并按模型和文本哈希复用向量。
默认限制为同一缓存/模型累计 40 次请求、200 条文本、40000 字符，重启不会清零。
本次基线首次构建消耗 23 次请求、80 条唯一文本、6003 字符；最终复跑使用缓存，新增请求为 0。
这不包含单独接口探测及产品验收索引产生的请求。字符数不等于计费 token，尚无已核对的真实费用。
缓存仅用于合成评测数据，不接管产品记忆，也不绕过产品的忘记语义。

## 复现命令

离线关键词基线不会请求模型，每次输出使用新目录：

```powershell
.\.venv\Scripts\python.exe -m evals.project_memory --mode lexical --output .artifacts/baseline/my-lexical
```

真实向量比较需要显式允许网络：

```powershell
.\.venv\Scripts\python.exe -m evals.project_memory --mode both --live --config infra/copilot.env --output .artifacts/baseline/my-real
```

运行器输出 `results.json` 与中文 `report.md`；默认新建隔离 SQLite 库，不使用产品数据库。
若要评测 PostgreSQL，额外设置 `SI_BASELINE_DATABASE_URL` 指向专用评测数据库。
运行器会新建独立 schema、执行生产迁移，并保留该 schema，便于核查；它不会自动删除 schema。
额度达到上限时运行器输出 incomplete，不会偷偷切换演示向量或把半成品标为完成。

按需复测接口，避免无必要地反复调用全部模型：

```powershell
.\.venv\Scripts\python.exe scripts/probe_copilot_models.py --config infra/copilot.env --output .artifacts/baseline/my-probe.json --chat --tools
```

可选参数还包括 `--stream`、`--vision`、`--embedding`、`--enrichment`。
探测只使用合成输入，出错时只记录异常类型和状态码，不保存服务错误响应体或鉴权头。

## 程序验证

本机 Ruff 通过。领域契约、API、导入、记忆生命周期、Loop、控制台、统一记忆、Provider、
新基线和规则文档测试共 134 passed、89 skipped。跳过项包含本机未配置的 PostgreSQL 与可选 SDK。
新增用例验证限额跨重启生效、失败请求预留、模型缓存隔离、维度请求与拒绝错误维度、
混合维度检索、模型能力配置和前端子进程的密钥隔离。
关闭响应维度校验的内存变异会使对应测试失败，随后恢复正常校验。
PostgreSQL 专用用例还验证迁移保留旧向量、拒绝有损降级以及距离操作数隔离，由 GitHub CI 执行。

本机全仓测试在收集阶段遇到上游 `os.geteuid` 的 Windows 不兼容及默认 GBK 解码问题，未取得全仓通过结果。
本轮针对性测试使用 `python -X utf8 -m pytest`；全仓 Linux 测试与 Compose 冒烟以实际 CI 状态为准。
本轮不把真实模型批量 judge 加入离线 CI。

## 下一轮工作

1. 独立复核参考标签，在 PostgreSQL 上复跑关键词/hybrid，再接入原 FTS5 对照。
2. 评测无答案拒答、记忆留存、事实更新冲突和 Agent 工具循环，公开失败样本。
3. 将评测任务与结果接入中文 Eval Lab，核对真实延迟、usage 与费用来源。
4. 完成 Langfuse 实例联调及检索子阶段 span；Jev 按用户要求继续后置。
5. 保持 Mem0、Graphiti/Zep、OpenViking 适配器为后续逐项验收，当前不填充外部系统成绩。
