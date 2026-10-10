# Si-agent 第一周：领域契约与基线

第一周定义了子系统之间传递的数据与接口，并记录了原有 FTS5 检索基线。
契约使用 Python 标准库的 `dataclass`、`Protocol`、`typing`、`datetime`。
“无第三方依赖”只描述契约层，不代表整个项目没有第三方依赖。

## 领域契约的含义与作用

契约约定调用需要哪些信息、返回值是什么、哪些状态合法。调用方依赖这些
约定，实现方可以替换数据库、模型 SDK 或记忆后端。

| 契约 | 所在文件 | 表达的内容 |
|---|---|---|
| `Budget` | `waku/domain/contracts.py` | 迭代、token、工具次数与超时限制 |
| `RunContext` | 同上 | run、用户、项目、会话、模型、预算与策略 |
| `ToolResult` | 同上 | 成功、失败、部分完成、结果未知，以及操作 ID |
| `MemoryNode` / `MemoryHit` | 同上 | 节点与包含 URI、来源、有效期的检索证据 |
| `MemoryPort` | `waku/memory/port.py` | 搜索、写入、读取、目录浏览与忘记接口 |
| `ProviderPort` | `waku/providers/contracts.py` | 模型调用、流式事件、能力和模型目录 |

例如运行层调用 `memory.search(MemoryQuery(...))`，收到 `MemoryHit` 即可读取
URI 与正文，不必知道底层是 SQLAlchemy、Mem0 还是其他服务。Provider 契约
同样避免让业务逻辑直接依赖某个厂商的 SDK 对象。

`Protocol` 表达接口形状，`dataclass` 承载数据。类型标注不等于完整运行时校验；
只有 `__post_init__` 等显式检查会在构造时执行。

## 已定义与已接入的区别

第一周创建了契约、基础校验和离线测试，没有全面重构旧 Loop。
`Budget` 能拒绝不合法的限制值，但不会自己中断工具或模型调用。
deadline、审批、重试和工具次数限制需要 Harness 主动执行。
`ToolResult` 尚未成为所有旧工具的统一返回格式。
`ProviderPort` 已定义，多厂商运行适配与 handoff 仍在后续计划。

第三周 `PostgresMemory` 已实现记忆读写与检索；忘记功能等待 tombstone。
因此不能把“接口存在”写成“所有能力已经上线”。

## FTS5 基线

运行 `python scripts/benchmark_memory_baseline.py`。脚本只使用内存数据库，
不读取或清空原有运行数据。

| 查询 | 基线行为 |
|---|---|
| `morning meetings` | 命中精确关键词 |
| `early-day syncs` | 未命中，不理解语义改写 |
| `Сергей` | 命中 Unicode 精确词 |
| 空查询 | 无结果 |

这份基线记录原行为，方便比较新方案。第三周证明向量分支能参与召回，尚未
用真实 embedding 模型完成有代表性的数据集质量基准。

产品名称为 Si-agent，虚拟目录采用 `si://`。代码暂时保留 `waku/` 包路径和
原兼容入口，作者署名与许可证保持可追溯。
继续阅读：[三周总览](copilot-progress.zh-CN.md)、[记忆设计](copilot-memory.md)。
