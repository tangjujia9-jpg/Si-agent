# Si-agent 后端与运行手册

Si-agent 通过 FastAPI 接收项目与资料，使用 PostgreSQL 持久化，通过独立 Worker
导入与索引，再由 React 展示检索结果。当前版本支持单用户多项目，后端已经接通
自主 Agent Loop 与模型生成；React 已提供对话工作台、按需资料/记忆侧栏和独立项目监控。

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

### 当前本机 PostgreSQL 环境

当前 API、Worker 已切换到 WSL Ubuntu 中的 PostgreSQL 16 / pgvector 0.8.2。
数据库集群名为 `si-agent`，端口为 55432；API 使用 8016，前端使用 5186。
连接串和真实模型密钥只保存在忽略的 `infra/copilot.env`，不要提交这个文件。

先启动数据库（本机已经安装集群，以下命令不安装软件、不清空数据）：

```powershell
wsl -d Ubuntu -u root -- bash -lc "pg_ctlcluster --skip-systemctl-redirect 16 si-agent start"
```

hy3 需要另一个终端保持 SSH 隧道：

```powershell
ssh -N -L 127.0.0.1:18790:127.0.0.1:18790 aliyun-tang
```

然后在仓库根目录的三个终端分别运行：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/run_copilot.py api --config infra/copilot.env
.venv/Scripts/python.exe -X utf8 scripts/run_copilot.py worker --config infra/copilot.env
.venv/Scripts/python.exe -X utf8 scripts/run_copilot.py web
```

访问 `http://127.0.0.1:5186`，监控入口为 `/monitor`，API 文档为 `http://127.0.0.1:8016/docs`。
`http://127.0.0.1:5186/ready` 检查代理和数据库，`/api/system` 可以确认实际后端为 `postgresql`。
只启动前端无法运行智能体；停止 Worker 会使任务停留在队列中。
该本机配置使用 hy3 和 Qwen 1024 维 Embedding，Jev 保持禁用。模型接入的历史记录见[7A 报告](copilot-week7a-release.md)。
2026-10-22 已将本机 `SI_COMPATIBLE_CONTEXT_WINDOW` 从 32,768 调整为 65,536；
实测范围与后续工具方案见[长任务上下文设计](si-context-and-tools-design.zh-CN.md)。

本轮新增迁移 0009，持久化工作摘要和来源依赖；升级前先备份数据库。上下文结构和预算结算见[接续验收](copilot-context-continuity-release.md)。

本机已设置 `SI_WORKSPACE_ROOTS=C:/code`，文件浏览器只允许在此范围内关联项目。
Windows 多个根目录使用分号分隔；Docker 默认不挂载宿主机目录，可以用浏览器导入快照。
若需要容器读取本地目录，必须明确只读挂载，并在 API 与 Worker 上配置相同的容器内路径。

### 将 Copilot SQLite 数据迁移到 PostgreSQL

本轮迁移保留了原 `.artifacts/dev/si-real-models.db` 和迁移前的 `.pre-postgres.db` 备份。
上游 `.waku/state.db` 属于另一套存储，不能用这个脚本直接转换。

迁移前停止使用源库的 API/Worker，并备份 SQLite 文件。将 `SI_DATABASE_URL` 明确设置为目标 PostgreSQL 连接串，再运行：

```powershell
.venv/Scripts/python.exe -X utf8 scripts/migrate_copilot_sqlite.py PATH_TO_COPILOT_SQLITE
```

脚本以只读方式打开源库，将目标迁移到 Alembic head，并拒绝覆盖非空目标。
数据复制、原生全文索引重建和逐项校验在同一目标事务内完成；失败会回滚。
主键、时间、版本、证据、遗忘屏障、向量、会话、运行、任务和操作关系保持不变。
确认成功后，API/Worker 必须同时切换目标数据库。脚本不删除源文件。

新项目仍优先使用 Compose，SQLite 只作为离线测试或明确选择的轻量适配器。

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
| `GET /api/runs/{id}/context` | 按需查看工作摘要、执行器状态、原文依赖与预算账本 |
| `GET /api/runs/{id}/events/stream` | 按序号重放 SSE，暂停或结束时返回快照 |
| `POST /api/operations/{id}/approval`、`POST /api/runs/{id}/cancel` | 批准/拒绝写工具与取消运行 |
| `GET /api/models` | 列举后端配置的模型与能力，不返回密钥 |
| `GET /api/system` | 实际数据库、pgvector 与目录访问能力，不返回连接串 |
| `GET /api/workspace/directories` | 浏览明确允许的项目目录 |
| `GET /api/projects/{id}/workspace`、`/workspace/file` | 项目文件清单与受限只读文本访问 |
| `GET /api/projects/{id}/monitor` | 最近运行和后台任务，独立监控页面的数据入口 |
| `GET /health`、`GET /ready` | 存活与就绪检查 |

当前界面支持文件与目录快照上传，也支持关联项目后的清单扫描与预览导入。
HTTP 导入协议仍是文本 documents 的路径与正文，尚未提供 Git 拉取、PDF、Word 或网页解析器。
路径校验拒绝绝对路径、上级跳转等输入。

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
| `0007_embedding_dimensions` | 放宽 vector 维度约束，保留旧向量并支持模型/维度隔离检索 |
| `0008_project_workspace` | 保存本地项目根路径与文件夹快照名称，保留原项目数据 |

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
