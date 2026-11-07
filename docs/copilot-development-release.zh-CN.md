# Si-agent 开发工具闭环验收

日期：2026-11-07。本轮实现 B 阶段：Agent 可以维护任务计划、读取和搜索文件、审批后修改文件、执行测试或构建，并检查 Git 差异。

## 用户如何使用

1. 在后端配置 `SI_DEVELOPMENT_ENABLED=true`，关联允许范围内的项目文件夹。
2. 在输入框选择“开发”，展开“项目开发权限”，分别允许申请文件修改、运行程序。
3. 提交任务。Agent 可以先更新计划、读取文件和查看 Git 状态；项目权限与本次模式共同决定模型可见工具。
4. 文件修改会显示具体 diff。用户批准后，执行器再次检查根目录、路径和文件哈希。
5. 程序执行会显示 argv、目录、执行环境、时限和项目快照哈希。用户批准后，Worker 执行持久化命令任务。
6. 对话显示命令状态、日志与退出码。用户可以停止命令或取消整个运行；Agent 在命令结束后继续检查结果。

分析模式继续只读项目文件。记忆修改仍走既有审批、证据与生命周期检查。开发模式不会替用户自动批准操作。

## 工具与上下文

产品执行器共有 17 个工具，API 校验、模式过滤和前端名称来自同一个能力目录。

| 工具 | 行为 |
|---|---|
| `list_project_files` / `read_project_file` / `search_project_files` | 列表、分段读取与字面搜索；返回哈希，支持目录/glob、分页及扫描覆盖说明 |
| `search_memory` / `read_memory` | 查询有效的项目证据和长期记忆 |
| `propose_memory` / `change_memory` / `forget_memory` | 保留已有的记忆审批、来源、版本和遗忘规则 |
| `read_operation_result` | 按操作 ID 回读持久化结果 |
| `git_status` / `git_diff` / `git_log` | 查看工作区修改、分页差异和最近提交；不提交、不推送、不清理修改 |
| `update_plan` | 更新步骤与进度；计划是模型的安排，不能证明工具执行成功 |
| `apply_project_patch` | 新建文本文件，或精确替换唯一匹配的旧内容；必须满足文件哈希前置条件 |
| `run_project_command` / `get_command_result` / `stop_project_command` | 审批后执行、查询分页日志、请求取消 |

执行器的真实状态、计划和命令结果进入现有工作上下文的任务状态区。摘要不会把计划完成改写为测试通过。
Agent 在命令运行期间暂停，Worker 完成后投递继续任务，避免模型通过循环查询浪费 token。
下一次请求可以看到命令状态、退出码和快照哈希；需要错误详情时再调用日志工具。
原有工作摘要、最近完整工具交换、独立检索证据、预算结算和遗忘检查继续生效。

开发预设为累计 256K token、20 次模型迭代、40 次工具调用、600 秒活动时限；单次模型窗口由 Provider 配置独立控制。
开发运行等待人工审批的时间单独累计，不消耗活动时限；命令本身另有最长 300 秒限制。
网络检索、网页抓取、Git 提交和推送尚未开放为产品工具。

## 文件修改如何恢复

迁移 `0010_development_tools` 增加项目权限、操作预览与命令任务。原项目默认没有文件修改或程序执行权限。
操作预览和审批绑定同一个哈希，旧页面不能批准不同的动作。
补丁保留原哈希、目标哈希、diff 和逐文件 journal；每个文件使用临时写入、fsync 和原子替换。
多文件修改不是数据库事务，中途失败会报告 `partial`、已完成项和待完成项。恢复时检查 journal 与实际哈希，不盲目重复替换。
用户已有修改不会被 Git reset、checkout 或整目录回滚清理；并发修改冲突会明确失败。
首版没有删除文件、搬移目录或自动撤销整项补丁的接口。

路径只接受受支持的项目文本文件，拒绝目录逃逸、符号链接、junction、硬链接、密钥路径和 Windows 特殊设备名。
`.gitignore`、`.dockerignore`、`.github/workflows`、Dockerfile 和 Makefile 可以按规则访问。
文件哈希检查不能锁住外部编辑器；本机恶意进程与最终检查之间的文件替换竞争不属于安全沙箱保证。

## 程序在哪里运行

默认 `SI_COMMAND_BACKEND=docker`。命令快照只复制支持的非敏感 UTF-8 文本，最多 5,000 个文件、20 MB；被排除的依赖目录和二进制不会悄悄复制。
审批后再次核对完整快照清单；文件变化就返回 `snapshot_stale`，要求重新审批。
容器使用非 root 用户、只读系统、无网络、禁用 capabilities、进程数和 CPU/内存限制；只挂载本次临时快照，不注入模型密钥、home、SSH agent 或 Docker socket。
容器镜像必须预先存在。执行时不自动拉镜像、安装依赖或执行联网安装脚本。

```powershell
docker build -f infra/Dockerfile.runner -t si-agent-runner:local .
# infra/copilot.env 中设置：
# SI_DEVELOPMENT_ENABLED=true
# SI_COMMAND_BACKEND=docker
# SI_COMMAND_IMAGE=si-agent-runner:local
.venv\Scripts\python.exe scripts/run_copilot.py api --config infra/copilot.env
.venv\Scripts\python.exe scripts/run_copilot.py worker --config infra/copilot.env
```

示例镜像含 Python、pytest 和 Ruff；Node、Go 等项目需管理员预先准备对应镜像。基础 Compose 保持开发权限关闭，不给常规 API/Worker 挂载 Docker socket。
容器执行需要运行在已配置 Docker CLI/daemon 的可信 Worker 主机；本机 Windows Worker 可以调用 Docker Desktop。
容器内产物只返回有界文件清单，不自动复制到真实项目；完整产物下载与用户选择导出仍待实现。

显式设置 `SI_COMMAND_BACKEND=trusted-local` 可以在临时工作副本执行 Python 或 Node。界面会说明它具有宿主文件和网络权限。
这个适配器只清理继承环境变量，不能阻止项目代码主动读取宿主文件，因此只适用于用户信任的项目。
Windows 使用 Job Object，在恢复项目代码执行前绑定进程树，关闭执行器时终止子进程；Linux 使用独立进程组。
容器不可用时会报告环境错误，不会自动使用可信本机模式。

## 持久化、取消和遗忘

命令 job 与操作 ID 一一对应，持久化状态、快照哈希、退出码和日志。重复查询或审批不会重复启动程序。
命令每隔短时间续租、保存日志和检查取消；模型在等待期间不轮询。
Worker 中断后不能确认结果的任务标为 `unknown`，不会自动重放 argv；容器只按本任务标签核验后清理。
容器清理不能确认时也会标记结果未知。日志最多保存 200,000 个字符，超限单独标注，分页同时限制 UTF-8 字节。
遗忘会清除关联操作预览、命令请求和日志，并请求停止任务；来源失效后不能通过日志接口绕过依赖检查。
凭据模式脱敏只降低常见泄漏风险，不能识别所有敏感内容。

## 验证与边界

确定性验收使用临时 Git 项目与脚本化模型，实际调用 API、Worker、文件系统和程序；测试不调用 Embedding 或 Jev。
两条完整路径分别验证接口校验修复与两项回归测试，以及构建失败、补齐配置和新快照重测。
其他用例验证已有修改保留、过期预览、跨项目拒绝、部分补丁恢复、取消、超时、大日志和中断不重放。
前端测试验证审批发送预览哈希，以及任务计划、命令日志和停止入口。
CI 单独构建执行镜像，实际验证非 root、只读根目录、无网络、无凭据继承和不回写工作区。

本机 Docker Desktop 启动时遇到自身 inference socket 错误，容器路径的最终验证以 Linux CI 记录为准；本机可信模式与 PostgreSQL 路径单独测试。
真实模型的工具规划质量、复杂项目的依赖镜像、完整产物导出与跨机器执行器仍需要后续验收。程序成功不能替代真实模型成功率或普遍安全性报告。

## 后续

继续交付独立 Si Memory MCP 接口、固定 PostgreSQL 检索基线、Eval Lab 和真实 Langfuse 联调。
开发工具后续增加可下载产物、专用执行节点和依赖镜像配置；联网与远程 Git 动作需要独立权限与预览。
