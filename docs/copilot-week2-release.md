# Si-agent 第二周验收报告：持久化项目与异步导入

第二周建立项目 API、PostgreSQL 存储、导入 Worker 和 React 控制台，完成
“创建项目 → 提交文档 → 查看任务 → 检查原文”的闭环。

## 完成内容

- PostgreSQL 保存项目、会话、消息、运行、上下文节点与任务，共六张表。
- SQLAlchemy 定义表映射、查询和事务边界，测试可替换连接。
- Alembic 管理版本迁移；历史迁移使用冻结 SQL，避免当前 ORM 模型改写旧版本。
- FastAPI 提供项目、导入、任务、目录、原文和会话创建接口，校验输入和项目归属。
- Worker 使用幂等键、内容哈希、行锁、租约、fencing token 和有限重试。
- 文档写入与任务成功状态在同一事务提交，失败时整体回滚。
- React 支持项目切换、文本提交、任务轮询、目录浏览与原文检查。
- Compose 编排数据库、迁移、API、Worker 和 Web，数据库使用持久化卷。
- GitHub CI 验证数据库、前端与完整 Compose 流程。

这轮主要建立存储和服务基础设施，并增加导入任务的可靠执行保障。
数据库选型服务于并发、持久化和后续检索，不能把全部改动都归为 Harness。
消息与运行表当时只是预留结构，没有接入新产品的完整 Agent 聊天。

## 当时的验证结果

| 检查 | 结果 |
|---|---|
| 新后端与相关旧功能回归 | 118 通过、49 跳过 |
| PostgreSQL | 真实 Postgres 16/pgvector、真实迁移、隔离 schema |
| 前端导入、轮询、目录与原文 | 1 通过 |
| TypeScript、生产构建、Ruff | 通过 |
| Alembic 模型与迁移一致性 | 通过 |
| Compose 配置、锁文件、运行 smoke | 通过 |
| 浏览器创建、导入、原文与项目切换 | 通过 |

[GitHub CI 37458564425](https://github.com/tangjujia9-jpg/Si-agent/actions/runs/37458564425)
验证了 `a4323a8` 的真实数据库与完整容器部署。本机 Docker Hub 请求超时
影响构建，Linux CI 随后完成镜像构建和 smoke。缺少凭证的外部适配器等用例
被跳过。原始更广测试集在 Windows coding-eval 模块仍有失败，不能声称整个
仓库全部通过。

第三周新增的分层检索见[第三周报告](copilot-week3-release.md)。当前产品 URI
使用 `si://`；历史提交的旧前缀通过 `0003_si_namespace` 迁移更新，保留原文、
节点 ID、证据关系和向量。
