# Si-agent 后端与运行手册

Si-agent 通过 FastAPI 接收项目与资料，使用 PostgreSQL 持久化，通过独立 Worker
导入与索引，再由 React 展示检索结果。当前版本支持单用户多项目，后端已经接通
自主 Agent Loop 与模型生成；React 已提供聊天、记忆审核、检查台与 Run Trace。

## 组件职责

| 组件 | 职责 | 代码入口 |
|---|---|---|
| FastAPI | 校验请求、隔离项目、提交任务、返回检索与原文 | `waku/server/app.py` |
| PostgreSQL | 保存关系数据、全文索引、向量、任务与幂等凭据 | `waku/storage/models.py` |
| SQLAlchemy | 将 Python 操作转换为 SQL，管理连接、事务与 ORM 映射 | `waku/storage/database.py` |
| Alembic | 根据版本号执行数据库结构和数据迁移 | `infra/migrations/` |
| Worker | 领取任务、维护租约、执行导入与向量索引、失败重试 | `waku/workers/runner.py` |
| React | 创建项目、导入资料、聊天、审批、记忆检查与轨迹 | `apps/web/src/` |

SQLAlchemy 和 Alembic 都不是数据库。原 SQLite 适合单机轻量运行，支持事务与
FTS5；PostgreSQL 适合 API、多个 Worker 同时访问，提供行锁、`SKIP LOCKED`、
全文检索和 pgvector。SQLite 适配路径继续服务于轻量测试，原 `.waku/state.db`
保持独立。本次迁移不自动搬运原助手的聊天或语义记忆。

## 启动完整环境

在仓库根目录执行：

```powershell
docker compose -f infra/compose.yaml up -d --build
```

打开 `http://127.0.0.1:8080`，API 文档在 `http://127.0.0.1:8000/docs`。
Compose 按顺序启动数据库、迁移、API、Worker 和 Web，数据库使用持久卷。
`/health` 检查进程，`/ready` 检查数据库连接与关键表。

```powershell
python scripts/smoke_copilot.py
docker compose -f infra/compose.yaml logs --tail 100 api worker migrate
docker compose -f infra/compose.yaml down
```

`down` 保留数据库卷。不要添加 `-v` 来处理启动问题，它会删除资料。
本地数据库默认口令只适合开发，不应直接用于公共部署。

## 本机开发

### 重启现有中文演示环境

2026-10-18 的本机演示复用了上一轮临时 SQLite 数据库。前端地址为
`http://127.0.0.1:5186`，后端文档地址为 `http://127.0.0.1:8016/docs`。
该模式用于本机体验；PostgreSQL/pgvector 的完整环境仍按上面的 Compose 命令启动。
临时数据库不适合长期保管项目资料，也不能代替 PostgreSQL 的并发、全文与向量索引验收。

已经安装依赖时，可以分别在三个 PowerShell 终端启动下列进程。
前两个终端必须使用同一个数据库地址与向量配置。下面命令只复用已有数据库；
若数据库不存在，请使用 Compose 环境，不要把生产 Alembic 迁移直接应用到 SQLite。

终端一启动 API：

```powershell
cd C:\code\agent-study\waku-agent
$siDemoDb = 'C:\Users\Tang\AppData\Local\Temp\si-week6-34fdc9454ffa44b4a092b309afef0a44.db'
if (!(Test-Path -LiteralPath $siDemoDb)) { throw '演示库不存在，请使用 Docker Compose 启动完整环境。' }
$env:SI_DATABASE_URL = 'sqlite:///' + $siDemoDb.Replace('\', '/')
$env:SI_EMBEDDING_BACKEND = 'hash-demo'
& .\.venv\Scripts\python.exe -m uvicorn waku.server.app:create_app --factory --host 127.0.0.1 --port 8016
```

终端二启动 Worker：

```powershell
cd C:\code\agent-study\waku-agent
$siDemoDb = 'C:\Users\Tang\AppData\Local\Temp\si-week6-34fdc9454ffa44b4a092b309afef0a44.db'
if (!(Test-Path -LiteralPath $siDemoDb)) { throw '演示库不存在，请使用 Docker Compose 启动完整环境。' }
$env:SI_DATABASE_URL = 'sqlite:///' + $siDemoDb.Replace('\', '/')
$env:SI_EMBEDDING_BACKEND = 'hash-demo'
& .\.venv\Scripts\python.exe -m waku.workers.runner
```

终端三启动前端：

```powershell
cd C:\code\agent-study\waku-agent\apps\web
$env:SI_API_URL = 'http://127.0.0.1:8016'
npm run dev -- --port 5186 --strictPort
```

本次执行环境没有可用的 npm 命令入口，但已有 Node 与 node_modules，因此实际使用了等价的启动命令：

```powershell
& 'C:\Users\Tang\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5186 --strictPort
```

前端通过 Vite 将 `/api` 与 `/ready` 代理到 8016。仅启动前端只能显示页面，
停止 Worker 会使导入、抽取与 Agent 运行停留在队列中。
可以访问 `http://127.0.0.1:5186/ready` 检查前端代理与数据库就绪情况。
正常返回后，打开页面并新建项目或切换已有项目即可。终端前台运行时，按 Ctrl+C 停止对应服务。
`hash-demo` 使用演示向量，不提供真实语义检索能力。

### 使用 PostgreSQL 开发

安装 `uv sync --frozen --extra dev --extra copilot`。
将 `SI_DATABASE_URL` 设置为自己的 PostgreSQL SQLAlchemy URL，例如：

```powershell
$env:SI_DATABASE_URL = 'postgresql+psycopg://si:si-local@127.0.0.1:5432/si_agent'
uv run --no-sync alembic upgrade head
uv run --no-sync uvicorn waku.server.app:create_app --factory --port 8000
```

在另一个终端使用相同数据库和 embedding 配置启动
`uv run --no-sync python -m waku.workers.runner`。
在 `apps/web` 下执行 `npm ci`、`npm run dev`，Vite 的 `SI_API_URL` 指向 API，
默认 `http://127.0.0.1:8000`。密钥只放在后端环境，不能使用 `VITE_` 变量暴露。

SQLite 测试通过 `Base.metadata.create_all` 创建独立临时数据库；正式迁移的
`0001` 包含 PostgreSQL 类型与扩展，不能把这套生产迁移直接当作通用 SQLite 初始化。

## 当前接口

| 方法与路径 | 行为 |
|---|---|
| `POST /api/projects`、`GET /api/projects` | 创建与列举项目 |
| `POST /api/projects/{id}/ingest` | 接受文本形式的 documents，返回 202 与任务 |
| `GET /api/jobs/{id}` | 查询任务与结果；导入结果包含独立 `index_job_id` |
| `GET /api/projects/{id}/context/tree` | 列举有效目录和资料节点 |
| `POST /api/projects/{id}/context/reindex` | 刷新层级表示并排队重建向量 |
| `GET /api/memory/search` | 查询词、项目、层级、预算与有效时间检索 |
| `GET /api/memory/{node_id}` | 读取原文、URI、来源事件与 revision |
| `POST /api/threads` | 创建会话记录 |
| `POST /api/threads/{id}/memory/distill` | 将该会话当前增量立即排队蒸馏，返回 job |
| `POST /api/threads/{id}/messages` | 幂等排队 Agent 运行，返回 202 |
| `GET /api/runs/{id}`、`GET /api/runs/{id}/events` | 查询运行状态、预算、工具和持久化事件 |
| `GET /api/runs/{id}/events/stream` | 按序号重放 SSE，暂停或结束时返回快照 |
| `POST /api/operations/{id}/approval`、`POST /api/runs/{id}/cancel` | 批准/拒绝写工具与取消运行 |
| `GET /api/models` | 列举后端配置的模型与能力，不返回密钥 |
| `GET /health`、`GET /ready` | 存活与就绪检查 |

当前导入接受 Markdown、文本和部分代码后缀的路径与正文；界面是粘贴表单，
尚未提供 Git 仓库、目录上传、PDF 或网页解析器。路径校验拒绝绝对路径、上级跳转等输入。

## 任务可靠性与 Harness

导入请求使用项目内唯一幂等键和 payload hash。相同键与相同资料返回同一任务；
相同键与不同资料返回 409。数据库唯一约束处理并发请求，API 使用 savepoint
捕获冲突，避免破坏外层事务。

Worker 使用任务状态、租约时间和 lease token。PostgreSQL 行锁与 `SKIP LOCKED`
让多个 Worker 分别领取任务；过期租约允许恢复，旧 token 无权提交结果。失败任务
有限重试，避免无限循环。向量计算放在事务外，并在提交前再次检查租约与内容版本。
这些机制属于 Harness 的执行可靠性部分；数据库选型本身不是完整 Harness。
第五周已接通模型预算、工具审批、工具 outbox 和 Run checkpoint，详情见
[运行时手册](copilot-runtime.md)。

## 数据表与迁移

第二周建立 `projects`、`threads`、`messages`、`runs`、`context_nodes` 和 `jobs`。
第三周增加 `context_indexes` 与 `memory_writes`。第五周已使用 `messages`、`runs`
驱动独立的项目 Loop，并增加事件/operation/outbox。第四周已增加 `memory_versions`、`memory_evidence`、
`memory_candidates` 和 `forget_tombstones`，版本和证据接口可以审计记忆变化。

| 版本 | 内容 |
|---|---|
| `0001_copilot` | 初始关系表与 pgvector 扩展 |
| `0002_memory_indexes` | L0/L1/L2 索引与幂等写入记录 |
| `0003_si_namespace` | 将节点 URI、父目录 URI 与导入结果根 URI 改为 `si://` |
| `0004_memory_lifecycle` | 分块索引、历史/证据/候选/tombstone，并为旧项目排队 rebuild |
| `0005_runtime` | 暂停/unknown 状态、有序运行事件、工具操作与 outbox |
| `0006_unified_memory` | 消息序号、水位、摘要表示版本和派生记忆重审标记 |

第三个迁移保留原文、节点 ID、来源事件、revision 与向量。URI 碰撞会让事务失败，
不会覆盖已有节点。旧 URI 的客户端书签需要更新；原写入请求的 payload hash
保留审计含义，改用新 URI 发起写入时请使用新的幂等键。

## CI 与验收边界

`.github/workflows/copilot.yml` 在 `main`、`codex/**` 的 push 与 PR 上运行。
第一个任务运行 Ruff、领域/API/Worker/检索/规则文档测试、前端测试和构建，
使用真实 `pgvector/pgvector:pg16` 服务，并为用例建立独立 schema。
第二个任务构建整个 Compose，执行 `scripts/smoke_copilot.py`，验证导入、索引、
引用检索、离线 Agent 回答与 SSE trace；失败保留日志，最后停止容器且不删卷。

上游另有全量确定性测试/技能验证、hosted Docker 与 tag 发布工作流。它们的
范围、触发条件和新 Copilot 工作流不同；不能把一次 Copilot 成功当作全仓库验证。
Judge 与付费 embedding 质量评测不在此离线流程内。

中文验收结果见 [第二周](copilot-week2-release.md)、[第三周](copilot-week3-release.md)
与[第四周](copilot-week4-release.md)、[第五周](copilot-week5-release.md)。前三周背景见[源码导读](copilot-progress.zh-CN.md)。

第四周新增的 versions/evidence/candidates/apply/forget 接口及 Worker 的
enrich/consolidate/rebuild 流程见[第四周报告](copilot-week4-release.md)。升级后执行
`alembic upgrade head` 并启动新 Worker，迁移会为既有项目排队分块重建；
等待任务完成后再验收向量覆盖。不要用 `create_all` 替代已有库的结构迁移。

统一阶段的接口默认值、会话来源与审核流程见[统一记忆报告](copilot-unified-memory-release.md)。
升级前先停止旧 API 与 Worker，再执行迁移；旧代码不能与新的版本规则混用。
