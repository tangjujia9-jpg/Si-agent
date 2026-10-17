# Si-agent 第六周开发与验收

日期：2026-10-17。本轮将统一记忆后端接入可操作的 React 工作台，并补齐会话辅助抽取与显式修正。
本轮保持 PostgreSQL/pgvector 主路径，保留旧 CLI，不搬运或删除已有用户数据库。

## 本轮交付

| 方面 | 本轮行为 |
|---|---|
| Chat | 创建与切换会话、选择后端模型、加载历史、发送消息、引用打开、取消运行 |
| SSE | 恢复持久化游标、去重重放事件、连接中断提示、审批期间持续快照、终态关闭 |
| 审批 | 展示工具参数与结果，人工批准/拒绝，再由原 outbox 驱动执行与恢复 |
| Memory review | 待审、接受、跳过、变更前后内容、证据打开、冲突原因、后台任务轨迹 |
| Inspector | L0/L1/L2、正文/表示版本、引用来源、历史对比、待复核、修正、撤回和忘记预览 |
| 会话抽取 | 当前用户消息附带有界用户历史与有效记忆，解析后仍需当前消息的精确引用 |
| 事实对齐 | 同键命中有效节点后生成 UPDATE，携带 expected_revision；模型可以指定给定上下文中的目标 URI |
| 修正 | 新用户陈述成为独立来源，生成待审 UPDATE，不拿旧事实的证据冒充新结论的依据 |
| Agent 工具 | 增加受控 change_memory/forget_memory，按 Run 显式暴露，写操作经过审批 |
| Tracing | 增加上下文编译 span；后台任务关联会话/候选，保留来源 ID，沿用可选 Langfuse OTLP |

SSE 展示运行进度与最终回答，当前没有逐 token 输出。模型选择只显示后端配置的目录，
前端不保存密钥，也不直接请求模型厂商。离线 evidence-demo 只列出召回引用，不生成推理结论。

## 记忆写入如何变化

```text
用户消息 → 同事务保存消息、不可变来源与精确证据、运行和延迟任务
  ├─ 在线：检索门 → 混合召回 → 上下文编译 → 模型/工具/审批 → 回答
  └─ 后台：会话增量 → 有界背景 → 抽取/留存 → 对齐事实 → 待审候选
                                                  ↓ 人工接受
                                           证据/版本校验 → 正式记忆
```

模型抽取以当前消息为目标，最多携带最近 4 条用户陈述及最近 8 条有效项目记忆。
上下文不会包含助手生成的陈述。当前消息必须提供精确 quote；辅助用户陈述和相关记忆的证据
成为保守依赖，提交时再次检查删除、正文版本和状态。被替代的旧事实本身不会成为新结论的支持证据。
辅助来源在模型调用期间变化时，任务拒绝提交并进入现有重试流程。

系统只做确定性同键对齐与模型建议目标检查，尚未实现普遍的事实实体归一、语义矛盾裁决或自动信任排序。
自动候选仍需人工接受。检索门与 Jev 留存策略沿用上一轮，Jev 不代替证据校验。
普通自然语言抽取需配置 SI_EXTRACTION_BACKEND=openai-compatible；默认离线标记模式保持可复现。

## 三种操作的区别

- 跳过候选：保留来源，候选不成为正式记忆。
- 撤回事实：更改事实状态并保留历史，已知派生事实进入复核。
- 忘记来源：脱敏选定节点及其历史证据和已知派生项，写 tombstone，清理受影响的检查点。

忘记派生事实不会反向删除独立的上游来源。忘记会话来源时才同时脱敏其原用户消息。
忘记不能撤销已经发给外部模型的请求，也不能保证识别尚未建立依赖的语义副本。
检查台先展示已知影响，再由用户确认；忘记工具要求人工审批与预期版本，来源更新后旧审批失效。

## 新增 API

```text
GET  /api/projects/{id}/threads
GET  /api/threads/{id}/messages?before={sequence}&limit=100
GET  /api/threads/{id}/runs
GET  /api/projects/{id}/jobs
GET  /api/projects/{id}/evidence?ids={evidence_ids}
POST /api/memory/candidates/{id}/skip
POST /api/memory/{id}/corrections
GET  /api/memory/{id}/forget-preview
```

corrections 接收 content、expected_revision、idempotency_key，记录新的用户来源并返回待审候选。
forget 可传 expected_revision，防止审批后误删新内容。已有消息提交、候选接受、SSE、审批与取消接口继续使用。
所有新查询都校验项目归属。本轮不增加数据库迁移；上一轮的 0006 仍是当前 head。

## 启动与演示

```powershell
cd C:\code\agent-study\waku-agent
docker compose -f infra/compose.yaml up -d --build
```

打开 http://127.0.0.1:8080，按以下步骤验收：

1. 创建项目，在 Context explorer 导入 `Decision[storage]: Use SQLite for the project database.`。
2. 打开 Memory review，检查来源并接受 storage 候选，等待 applied。
3. 打开结果节点，展开 Correct this memory，提交 PostgreSQL/pgvector 的新陈述。
4. 接受 UPDATE 候选，查看正文 revision 增加和历史前后对比。
5. 在 Conversation 创建新会话，选择 demo/evidence-demo，询问 PostgreSQL，打开 storage.md 引用。
6. 刷新页面，确认当前会话、消息和 Run Trace 恢复。接真实模型后可开启记忆变更工具并操作审批。
7. 在检查台预览忘记范围，再确认忘记；检查正文与历史脱敏、关联事实和运行状态。

实际后端模型质量、成本及延迟，需要另外配置真实模型后测试。
审批截止时间继续包含等待人工操作的时间，过期运行需要新建，不提供强制恢复 unknown 的入口。

## 验证方式

```powershell
uv run --no-sync pytest -q evals/deterministic/test_copilot_console.py
uv run --no-sync ruff check waku evals scripts hosted
cd apps/web
npm test
npm run build
```

浏览器验收脚本只创建新的演示项目，忘记操作只针对本次创建的演示记忆：

```powershell
pip install playwright==1.55.0
python -m playwright install chromium
python scripts/smoke_console.py
```

本机可用 `--browser-executable` 指定已安装的 Chrome，使用 `--url` 和 `--api-url` 连接自己的开发端口。
脚本检查导入、候选审核、修正、跨会话引用、刷新恢复、Run Trace、忘记与手机宽度布局。
截图保存在 .artifacts/console；CI 在 Compose 作业里运行并上传截图。

本轮本机已通过真实 Chrome 浏览器闭环，包含桌面和 390px 手机布局，无页面脚本错误。
本机浏览器使用独立临时 SQLite 测试库，不代替 CI 中 PostgreSQL/pgvector 的验证。
后端相关模块及规则文档回归通过 115 项，86 项因 PostgreSQL 或可选条件跳过。
前端 9 项测试、TypeScript 检查、Vite 生产构建与 Ruff 均通过。

GitHub 对实现提交 `d654365` 的[完整 CI 验证](https://github.com/tangjujia9-jpg/Si-agent/actions/runs/39001321777)已通过：

| 检查 | 结果 |
|---|---|
| 全量确定性回归，包含真实 PostgreSQL/pgvector | 2624 项通过，66 项因可选运行条件跳过 |
| 前端测试 | 3 个测试文件、9 项测试通过 |
| 静态检查与构建 | Ruff、TypeScript、Vite 生产构建通过 |
| Docker Compose | 数据库、API、Worker、Web 启动与后端 smoke 通过 |
| 浏览器完整闭环 | Chromium 完成导入、审核、纠错、跨会话引用、刷新恢复、轨迹与忘记操作 |
| 响应式布局 | 桌面和 390px 手机宽度验证通过，未检测到页面脚本错误 |

同一 CI 页面提供 `si-console-screenshots` 产物，保存四张浏览器验收截图。
这些结果验证离线演示链路及程序边界，不代表真实模型或外部观测服务的质量验收。

## 保留的边界

旧 CLI/SQLite 历史迁移、全局个人记忆、工具经验蒸馏、逐 token 输出和多租户鉴权尚未实现。
当前消息分页支持向前加载；会话列表最多 200 个、运行列表默认最近 50 个、候选最近 100 个。
界面复用后端已有的预算与状态协议，不提供自动绕过冲突、强制恢复未知操作或自动接受候选的入口。
真实 Jev/生成模型/Embedding 质量、Langfuse 在线服务联调与外部记忆后端对照仍进入第七周评测。
