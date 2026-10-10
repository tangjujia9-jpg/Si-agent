# Si-agent 后续开发与验收计划

前四周已实现契约、持久化导入、带引用检索与记忆生命周期。完整产品需要继续接入运行时，
补齐历史记忆、聊天、观测和质量评测。今后的验收报告使用中文，区分已实现、
已验证与待实现能力。

| 里程碑 | 开发内容 | 验收结果 |
|---|---|---|
| 第四周 | 已实现分块、可选模型摘要/抽取、candidate/evidence/version、冲突、tombstone | 验证边界见[第四周报告](copilot-week4-release.md) |
| 第五周 | 新记忆接入 Loop、Provider、工具校验、operation/outbox、LangGraph、Langfuse | 项目任务有完整调用链、预算控制与可恢复状态 |
| 第六周 | Chat/SSE、记忆检查台、Run Trace、模型配置 | 回答可打开来源与运行轨迹 |
| 第七周 | FTS5/hybrid 与外部适配器对比、检索与 Agent 指标、Eval Lab | 可重复评测质量、泄漏、过期、成本和延迟 |
| 第八周 | README、演示、CI、恢复、安全与许可审查、基准报告 | 面试官十分钟内复现闭环 |

## Langfuse 验收要求

Langfuse 是第五周优先观测后端。Si-agent 拥有事件协议、运行记录和评测标准，
Langfuse 通过可选适配器提供存储与查询；未启用时产品仍可运行。

- 每次运行有根 trace，关联用户、项目、会话、run 和模型。
- span 包围真实操作，记录异常、重试、取消和超时。
- 检索阶段包含 scope/gate、query embedding、L0/L1、两路召回、融合、L2 与预算。
- 导入和索引关联 job、node、revision 和 evidence ID。
- 摘要、consolidation、judge 等 Loop 外调用也统计 usage 与成本。
- 工具记录结构化状态与 operation ID，区分估算和实际收费。
- 持久化与导出前脱敏，原文和 prompt 收集可配置，密钥不进入事件。
- 导出异步且有界，关闭时 flush，失败不能破坏业务运行。
- 验证固定版本的 SDK/OTel 协议、鉴权与属性映射。
- 分数记录对应运行、证据、evaluator、版本、judge 模型与数据集。

确定性测试用内存 sink，集成测试用可选的自托管 Langfuse Compose profile。
验收检查父子关联、持续时间、并发、脱敏、导出降级和辅助调用 token 总数。
在线质量检查与运行时硬预算分别测试。
