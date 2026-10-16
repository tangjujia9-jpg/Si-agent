# Si-agent 后续开发与验收计划

前五周已实现契约、持久化导入、带引用检索、记忆生命周期和自主运行时。
完整产品需要补齐聊天与观测界面、会话自动沉淀和质量评测。验收报告使用中文，区分已实现、
已验证与待实现能力。

| 里程碑 | 开发内容 | 验收结果 |
|---|---|---|
| 第四周 | 已实现分块、可选模型摘要/抽取、candidate/evidence/version、冲突、tombstone | 验证边界见[第四周报告](copilot-week4-release.md) |
| 第五周 | 已实现新记忆接入 Loop、Provider、工具校验、operation/outbox、可选 LangGraph、Jev、Langfuse | 边界见[第五周报告](copilot-week5-release.md) |
| 统一阶段 | 已接通会话候选、统一留存、Run 检索复用和来源失效；继续补迁移与显式用户修正 | 见[统一记忆报告](copilot-unified-memory-release.md) |
| 第六周 | Chat/token delta、审批 UI、记忆检查台、Run Trace、模型配置、会话沉淀接线 | 回答可打开来源与运行轨迹 |
| 第七周 | FTS5/hybrid 与外部适配器对比、检索与 Agent 指标、Eval Lab | 可重复评测质量、泄漏、过期、成本和延迟 |
| 第八周 | README、演示、CI、恢复、安全与许可审查、基准报告 | 面试官十分钟内复现闭环 |

## Langfuse 验收要求

Langfuse 是第五周优先观测后端。Si-agent 拥有事件协议、运行记录和评测标准，
Langfuse 通过可选适配器提供存储与查询；未启用时产品仍可运行。

- 每次运行有根 trace，关联用户、项目、会话、run 和模型。
- span 包围真实操作，记录异常、重试、取消和超时。
- 检索阶段包含 scope/gate、query embedding、两路召回、融合、表示层级与预算。
- 导入和索引关联 job、node、revision 和 evidence ID。
- 摘要、consolidation、judge 等 Loop 外调用也统计 usage 与成本。
- 工具记录结构化状态与 operation ID，区分估算和实际收费。
- 持久化与导出前脱敏，原文和 prompt 收集可配置，密钥不进入事件。
- 导出异步且有界，关闭时 flush，失败不能破坏业务运行。
- 验证固定版本的 SDK/OTel 协议、鉴权与属性映射。
- 分数记录对应运行、证据、evaluator、版本、judge 模型与数据集。

第五周确定性测试覆盖内存 sink 和本地 OTLP HTTP/protobuf 接收端。
真实 Langfuse 自托管部署、细分 lexical/vector/RRF 观测、原文采集开关和 score 导出需要后续验收。
当前可将已有 Langfuse 实例地址注入 Worker；主 Compose 不包含 Langfuse 服务。
验收检查父子关联、持续时间、并发、脱敏、导出降级和辅助调用 token 总数。
在线质量检查与运行时硬预算分别测试。

## Jev 质量验收

第五周启用检索门和候选留存筛选，默认仍用本地规则。第七周用固定中文项目数据集
比较错误跳过检索率、稳定事实留存率、污染率、token 与端到端延迟。relevance 评分
先与当前 RRF 对照，再决定是否进入默认重排；置信度阈值必须以数据选择。
