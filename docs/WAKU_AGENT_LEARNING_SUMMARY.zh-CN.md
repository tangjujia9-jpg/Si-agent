+# Waku Agent 学习笔记

本文以 Waku Agent 提交 9660fb6 为学习对象。Waku 是一个本地优先的个人助手示例项目，它把一个可运行的 agent 拆成 Harness、Loop、Memory 和 Eval/LLM-Ops 四个主要部分，并用少量 Python 代码把这些部分连接起来。

## 1. 项目定位

Waku 的目标不是提供一个抽象度很高的 agent framework，而是提供一个可以在较短时间内读懂的 agent blueprint。项目默认使用本地 SQLite、FTS5 和本地文件，因此用户可以直接查看记忆、聊天记录、日历和 trace。

项目的主运行链是：

~~~text
Gateway 输入消息
  → Harness 组装运行环境
  → Memory 选择上下文
  → Loop 让模型思考并调用工具
  → Harness 保存结果和运行数据
  → Eval 检查行为是否改善
~~~

Graph workflow 是可选的编排层。它可以把多个步骤并行执行或显式路由，但它不会替代核心 Loop。

## 2. 顶层目录

~~~text
waku-agent/
├── waku/                  产品代码
│   ├── app.py             Waku 组装和一次 turn 的总入口
│   ├── config.py          配置和环境变量
│   ├── db.py              SQLite schema 和连接
│   ├── connect.py         外部连接命令
│   ├── integrations.py    集成状态
│   ├── gateway/           CLI、dashboard、voice、Telegram 等入口
│   ├── runtime/           Session 和 working memory
│   ├── loop/              LLM provider 适配和核心 agent loop
│   ├── tools/             工具定义、注册、执行和 MCP
│   ├── memory/            semantic、episodic、procedural memory
│   ├── graph/             Graph engine、节点和 workflow
│   └── ops/               dashboard、tracing、arena、release gate
├── evals/
│   ├── deterministic/     离线 0/1 测试
│   ├── judge/             LLM-as-judge 质量评估
│   ├── hosted_docker/     hosted 部署测试
│   └── memory_arena.json  记忆测试数据
├── docs/                  架构、教程、集成和设计文档
├── examples/              用于教学的小例子
├── lab/                   Mem0、Zep、LangMem 等实验
├── skills/                内置和社区 SKILL.md
├── hosted/                hosted 部署代码，和本地 waku 分开维护
├── scripts/               构建白板、验证和开发脚本
├── pyproject.toml         包、依赖、命令和可选 extra
└── AGENTS.md              仓库规则
~~~

最值得先读的文件顺序是：

~~~text
waku/app.py
  → waku/runtime/session.py
  → waku/memory/__init__.py
  → waku/loop/agent.py
  → waku/tools/registry.py
  → waku/db.py
  → waku/graph/engine.py
  → evals/deterministic/
~~~

## 3. 一次请求的完整生命周期

默认关闭 Graph 时，入口是 Waku.respond()：

~~~text
Gateway 收到用户消息
  → Waku.respond()
  → Session.build_system()
      → 读取 SOUL.md
      → 加入当前时间和模型身份
      → retrieval gate 判断是否读取长期记忆
      → 查询 semantic facts 和 episodic episodes
      → 匹配 procedural skills
  → 截取最近 history_turns 轮对话
  → 加入当前用户消息
  → run_loop()
      → 调用模型
      → 执行工具
      → 把 tool_result 放回 messages
      → 继续调用模型或结束
  → Session.add_exchange()
      → 更新内存中的 history
      → 写入 chat_log
  → maybe_consolidate()
      → 达到阈值时提炼 facts 和 episode
  → export_markdown()
      → 生成可读的 MEMORY.md
  → tracer 写入 trace
  → 返回回复
~~~

代码层的关键点是：

- system 保存本轮固定的行为规则、时间、memory context 和 Skill。
- messages 保存近期历史、当前用户消息以及本轮工具往返。
- tools 保存模型可以调用的工具 schema。
- state.db 保存跨 turn 的持久数据。
- Session.history 是有界的 working memory，不是永久历史。

## 4. Harness 的职责

Waku 的 Harness 不是一个单独文件，而是由 app.py、runtime/session.py、tools/registry.py、gateway 和 tracing 共同组成。

Harness 负责：

1. 接收不同来源的消息。
2. 创建数据库、模型 client、Memory、ToolRegistry 和 Session。
3. 决定本轮模型能看到哪些上下文。
4. 注册工具并执行模型请求。
5. 限制 history、loop iteration、工具能力和可选 Graph。
6. 将错误转换成模型或用户可以理解的信息。
7. 持久化聊天、memory、工具结果和 telemetry。
8. 通过 observer 将执行事件发送给 UI 和 trace。
9. 用 deterministic eval 和 judge eval 检查行为。

Waku 的核心边界是：

~~~text
模型负责判断和规划。
程序负责边界、执行、持久化、幂等和安全降级。
~~~

例如，模型可以决定调用 create_event，但程序仍然应该校验时间、检查重复、写入数据库并报告真实结果。

## 5. Loop 设计

waku/loop/agent.py 是最核心的执行文件。它基本实现了：

~~~python
while not done:
    response = llm(messages, tools)

    if response has no tool call:
        return reply

    messages.append(assistant_response)

    for tool_call in response:
        output = execute_tool(tool_call)
        tool_results.append(output)

    messages.append(tool_results)
~~~

Loop 有两个结束条件：

1. 模型不再请求工具，进入自然结束。
2. 到达 max_iterations，进入硬停止。

Loop 的优点是简单、透明、可测试。它没有把控制流隐藏在大型框架中。

Loop 的局限是：

- 每次发送全部注册工具 schema，工具多时 prompt 会变大。
- 工具重试主要依赖模型再次调用，没有通用的结构化 retry policy。
- messages 会包含很长的工具输出，当前主要按轮数限制，而不是严格按 token 限制。
- 一次工具操作的业务状态没有独立任务表，复杂任务的恢复能力有限。

## 6. Memory 的四个层次

### 6.1 Working memory

Session.history 保存当前会话最近若干轮，默认是 12 轮。旧消息仍在 chat_log，但不会无限进入 prompt。

Working memory 解决当前对话中的指代、最近动作和局部任务上下文。

### 6.2 Semantic memory

facts 表保存长期事实：

~~~text
subject
content
source
created_at
~~~

默认通过 SQLite FTS5 查询。Waku 也实现了 Supabase、Mem0、Zep 和 LangMem 的 FactStore 适配器。

Semantic memory 适合保存用户偏好、人物信息、项目事实和稳定约束。

当前 SQLite 实现主要是关键词检索，因此它对同义表达、跨语言表达和事实冲突的处理能力有限。

### 6.3 Episodic memory

episodes 表保存带日期的事件摘要：

~~~text
happened_at
summary
created_at
~~~

它回答过去发生过什么。

Episode 检索将 FTS 排名和日期排序结合起来。当前 consolidation 使用整理当天作为 happened_at，所以真实事件日期仍然需要进一步建模。

### 6.4 Procedural memory

skills/ 中的 SKILL.md 保存做事流程。Skill 的 frontmatter 会被扫描，正文只在消息匹配时注入 prompt。

Waku 使用名称和 description 的关键词重叠触发 Skill，最多加载两个。这个实现透明、低成本，但当前主要支持英文关键词，无法很好处理中文和语义同义词。

### 6.5 SOUL.md

SOUL.md 不是普通事实，而是全局行为规则，例如：

~~~text
遇到长期偏好时保存事实。
创建日程时使用 create_event。
工具结果没有说明已同步时，不要声称已经同步。
~~~

它每轮进入 system prompt，因此它和按需加载的 Skill 不同：

~~~text
SOUL：全局、持续生效的行为规则。
Skill：特定任务、条件触发的操作流程。
~~~

## 7. Memory 读取路径

读取路径位于 Memory.gated_retrieve()：

~~~text
当前消息
  → retrieval_gate
  → retrieve = false：跳过 facts 和 episodes
  → retrieve = true：使用 gate 生成的 query 搜索
  → 拼接结果
  → 放入 system prompt
~~~

Gate 的目的不是提高搜索算法本身，而是决定本轮是否值得访问用户的长期记忆。

这样可以减少两类问题：

- 每个问题都检索，增加无关上下文。
- 不相关记忆进入 prompt 后影响主模型。

Gate 也有代价：

- 它额外调用一次小模型。
- 当前只看到用户当前消息，不一定理解历史指代。
- retrieve=true 但 query 很差时，仍然会检索不到有用内容。
- 当前输出解析对字段类型的校验不够严格。

因此检索质量至少要分成：

~~~text
gate decision
query quality
retrieval hit quality
answer usage quality
~~~

## 8. Memory 写入和 consolidation

Waku 支持两条写入路径。

### 8.1 显式保存

用户说“记住 Alex 喜欢上午开会”，模型可以调用 save_note，立即写入事实。

### 8.2 批量整理

consolidation.py 查询：

~~~sql
SELECT id, role, content
FROM chat_log
WHERE consolidated = 0
ORDER BY id
~~~

达到默认的 6 次交流后，小模型从未整理的聊天中提炼：

~~~json
{
  "facts": [
    {
      "subject": "Alex",
      "content": "Alex prefers morning meetings."
    }
  ],
  "episode": "Planned a meeting with Alex."
}
~~~

然后分别写入 facts 和 episodes，并把本次读取的 chat_log 行标记为已整理。

这套设计的优点是：

- 小于阈值时不调用整理模型。
- 模型失败或 JSON 无法解析时保留原始日志。
- 原始 chat_log 可以继续作为证据。
- facts 和 episodes 分开保存。

当前实现的不足是：

- 写入主要是 append，没有自动去重和冲突解决。
- consolidation 在 respond() 中同步执行，文档中的异步描述与当前调用路径不完全一致。
- 处理全部未整理日志，积压过大时需要 token 和批次上限。
- fact 写入中途失败时，已写入部分可能在重试时重复。
- episode 日期默认是整理日期。
- save_note 直接写 SQLite，而部分后端 consolidation 通过 FactStore 写入，替换后端时需要检查所有写路径。
- 用户要求忘记一个事实后，原始 chat_log 仍可能在未来再次提炼出相似事实。

更成熟的 consolidation 应把流程拆成：

~~~text
读取带 ID 的原始证据
  → 生成候选记忆
  → 校验字段和来源引用
  → 查询已有记忆
  → 判断新增、更新、冲突、过期或跳过
  → 用幂等 ID 应用写入
  → 标记处理结果
~~~

## 9. Tools 和动作状态

Tool 在 waku/tools/registry.py 中由三部分组成：

~~~text
name
description
input_schema
fn
~~~

模型看到 schema，程序调用 fn。

ToolRegistry 会把工具异常转成文本，让模型继续观察错误并决定是否修正参数。但是当前 registry 没有自动执行 JSON Schema 校验，业务参数仍然需要工具函数自己检查。

Waku 的 create_event 展示了几个重要的 harness 设计：

- 缺少标题或开始时间时返回可恢复的错误。
- 没有结束时间时默认一小时。
- 标准化时间格式。
- 对相同标题和开始时间做幂等检查。
- 先保存本地 SQLite，再写 ICS，再可选同步 Google 或 Apple Calendar。
- 外部同步失败时保留本地事件，并返回部分成功信息。

这条路径也暴露了不足：

- SQLite、ICS 和外部服务没有共同事务。
- 本地写成功、ICS 写失败时，重试会因为幂等检查而提前返回，无法自动补齐 ICS。
- 外部同步超时可能处于“结果未知”，不能简单认为没有创建。
- 默认工具没有完整的 update_event / delete_event 资源生命周期。
- tool_use_id 只标识一次模型请求，不能替代跨重试的 operation_id。
- 复杂任务需要独立的任务状态、资源 ID、步骤状态和 outbox。

适合长期系统的结构化结果可以包含：

~~~text
status: succeeded | failed | partial | unknown
operation_id
resource_id
completed_steps
pending_steps
retryable
error_code
message
~~~

## 10. Graph Workflow

Graph 位于 waku/graph/。它使用一个普通 dict 作为 blackboard，使用节点、边和 router 表达编排。

当前 triage 图是：

~~~text
START
  ├── classify
  └── check_calendar
          ↓
        gather
          ↓
        route
       ├── quick_reply
       └── full_agent
~~~

Graph engine 负责：

- 找到已经满足依赖的节点。
- 同一 wave 中并行执行 ready nodes。
- 等待 fan-in 节点的所有上游完成。
- 合并节点返回的 state keys。
- 检查并行写入冲突。
- 执行代码 router。
- 限制每节点访问次数和全局步骤数。
- 收集节点错误。
- 发出 graph_start、node_start、node_end、route、graph_end 事件。

full_agent 节点调用的仍然是普通 _run_full_turn()，所以 Graph 是控制平面，Loop 是执行平面。

Graph 的重要设计选择包括：

~~~text
routers 是代码，不是模型。
parallel branches 必须写不相交的 keys。
graph 失败时退回 plain loop。
graph topology 从真实 Graph.describe() 生成。
~~~

当前实现有一个值得注意的取舍：check_calendar 的结果主要供 quick_reply 使用，full path 当前只把 message 传给普通 loop。因此 full 请求可能提前读取了日历，但没有真正把结果注入 full agent。这说明预取只有在后续节点消费结果时才有价值。

## 11. Provider 和 Gateway 架构

### Provider

waku/loop/models.py 把不同厂商适配成 Anthropic Messages 风格：

~~~text
Anthropic
OpenAI
Gemini
DeepSeek
MiniMax
Kimi
GLM
OpenRouter
OpenCode
~~~

Loop 只依赖统一的 client 形状，因此 provider 切换不需要重写 loop。

### Gateway

waku/gateway/ 负责把外部消息送进同一个 Waku 实例：

~~~text
cli.py
voice.py
telegram.py
discord.py
whatsapp.py
runner.py
supervisor.py
~~~

Gateway 主要负责输入输出，不应该复制 memory、loop 或工具逻辑。

### Dashboard

waku/ops/dashboard.py 和 waku/ops/static/ 提供本地 dashboard，展示：

~~~text
Overview
Gateway
Loop
Graph
Memory
Tools
Data
Ops
~~~

Dashboard 读取真实状态和 trace，因此它可以作为学习系统运行过程的观察窗口。

## 12. Tracing 和 Eval

Waku 将评估分成两层。

### Deterministic eval

evals/deterministic/ 使用脚本化模型，检查 0/1 行为：

~~~text
正确工具是否被调用？
日历行是否写入？
history 是否被限制？
gate 解析失败是否 fail-open？
循环是否在 max_iterations 停止？
Graph 是否等待所有并行分支？
并行节点是否发生 state collision？
~~~

这些测试主要测试 Waku 自己的程序逻辑。

### Judge eval

evals/judge/ 使用 LLM-as-judge 评估：

~~~text
回答是否有帮助？
回答是否使用了检索记忆？
gate 是否合理？
query 是否足以找回相关记忆？
~~~

Judge eval 适合模糊的语言质量，但不能替代 deterministic eval。

### Trace

waku/ops/tracing.py 默认把每次运行写成 JSONL：

~~~text
turn_start
gate
llm
tool
consolidation
turn_end
~~~

如果配置了 OpenTelemetry，Waku 还可以把同一批事件发送到 Phoenix 或 Langfuse。

Waku 不只记录最终答案，还记录中间决策和 token 使用，因此可以区分：

~~~text
gate 跳过了记忆
query 没找到事实
找到了事实但主模型没有使用
工具执行失败
回答本身质量不足
~~~

## 13. 项目的核心设计思想

### 13.1 先做清晰的最小 loop

Waku 没有从大型框架开始，而是先把 LLM → tool call → tool result → LLM 写成可以直接阅读的循环。

### 13.2 Working memory 与 persistent memory 分离

当前会话只保留有限 history，长期信息进入 facts、episodes 和 skills。这样可以控制上下文成本，同时保留跨会话能力。

### 13.3 检索前先做 gate

Waku 不默认每轮检索长期记忆。它用一个小模型判断是否值得检索，以减少无关记忆对答案的干扰。

### 13.4 记忆类型按用途拆分

事实、事件、流程和全局行为规则分别保存，而不是全部塞进一个 MEMORY.md。

### 13.5 让模型做判断，让代码守边界

模型可以生成 query、选择工具和提炼候选；程序负责 schema、幂等、数据库、路由、循环限制、错误和持久化。

### 13.6 Graph 只包住 Loop

Graph 增加并行和路由，但不复制核心 loop。这样图 workflow 不会因为维护两套 agent 逻辑而逐渐漂移。

### 13.7 默认能力简单，升级路径明确

Waku 提供：

~~~text
SQLite FTS5 → Supabase pgvector
本地 episode → Notion
单一 provider → 多 provider
JSONL → OpenTelemetry
普通 loop → Graph workflow
~~~

默认安装保持低成本，复杂能力使用可选 extra 或配置开启。

### 13.8 Eval 和可观测性属于产品能力

Waku 将 trace、deterministic eval、judge eval 和 release gate 一起放进项目，而不是在 agent 做完以后再补监控。

## 14. Waku 的优点

1. 架构清晰。文件结构基本对应白板上的架构框。
2. Loop 简单。读者可以直接理解模型、工具和结果的循环。
3. Memory 分层合理。facts、episodes、skills 和 SOUL 各自承担不同职责。
4. Harness 责任明确。程序层维护上下文、工具边界、持久化和 tracing。
5. 默认本地优先。SQLite 和 FTS5 不需要云服务。
6. 后端替换接口清楚。FactStore 让 Mem0、Zep、LangMem 和 Supabase 可以接入。
7. Graph 不破坏默认路径。full agent 仍调用同一个 loop。
8. 错误通常会被显式记录。工具错误、gate 失败和 graph 错误不会完全静默。
9. 评估体系完整。程序行为和语言质量分别评估。
10. 教学价值高。项目把复杂 agent 系统压缩成了可以逐层阅读的代码。

## 15. Waku 的不足和改进方向

### Memory 质量

- SQLite FTS5 主要依赖关键词，语义检索和跨语言检索有限。
- facts 默认允许重复和冲突共存。
- consolidation 没有稳定的事实版本、时间有效期和证据引用。
- 用户删除记忆后，原始聊天仍可能再次生成同样事实。
- facts、episodes 和 skills 的检索结果缺少统一的结构化重排层。

### 写入可靠性

- consolidation 的事实写入和 chat_log 标记不是一个完整的幂等事务。
- 工具的本地写入、文件导出和外部同步可能部分成功。
- 外部调用超时后缺少“结果未知”的状态。
- 复杂任务缺少独立 checkpoint 和恢复机制。

### 上下文管理

- history 主要按轮数限制，而不是按 token 预算限制。
- 工具输出可能过长。
- 检索结果直接拼成字符串，缺少 score、source、time 和 evidence metadata。
- Skill 的触发器当前不适合中文和复杂语义。

### Agent 控制

- 所有注册工具默认进入每次模型请求，工具规模扩大后成本会上升。
- 工具参数 schema 没有在执行前统一验证。
- 主 loop 的通用 retry policy 不够结构化。
- Graph 当前没有跨进程 checkpoint、暂停恢复和人类审批节点。

### 可观测性

- gate 和 consolidation 的模型调用没有完全沿用 loop 的 token 记录路径。
- 当前 latency 统计不一定覆盖所有后处理阶段。
- 检索命中的结构化证据没有完整进入 trace。
- judge eval 仍然受到模型和 provider 漂移影响。

## 16. 与 Mem0、OpenViking 的组合方向

Waku 提供了清晰的 agent runtime 和 harness。Mem0 可以加强记忆提炼、更新和语义召回。OpenViking 可以补充统一 namespace、目录范围检索和 L0/L1/L2 分层上下文。

一个可行的组合架构是：

~~~text
Waku Harness
  ├── Session / Loop / Tools / Graph / Eval
  └── Context Memory Service
        ├── user/{id}/preferences
        ├── user/{id}/episodes
        ├── projects/{name}/
        ├── skills/
        └── sessions/
~~~

检索过程可以升级为：

~~~text
gate
  → 确定 user/project scope
  → 读取目录摘要
  → 选择相关节点
  → 打开少量详情
  → 按 token 预算注入 system
~~~

写入过程可以升级为：

~~~text
原始事件
  → 带来源的候选记忆
  → 去重和冲突判断
  → 版本与有效期
  → 幂等应用
  → 可审计的 evidence links
~~~

不要只把 Mem0 或 OpenViking 当成一个新的 search() 实现。它们改变的是 memory 的提炼、组织、更新和检索策略，接口统一不能抹平这些设计差异。

## 17. 建议的学习和实现路线

### 阶段一：复现 Waku 基础闭环

实现：

~~~text
SQLite chat_log
facts
retrieval gate
短 history
run_loop
save_note
~~~

目标是能解释每一条消息如何进入模型。

### 阶段二：加强记忆质量

加入：

~~~text
事实 ID
来源消息 ID
created_at / valid_from / valid_to
去重
冲突关系
统一 reranker
结构化 MemoryHit
~~~

### 阶段三：加强写入可靠性

加入：

~~~text
consolidation job table
idempotency key
outbox
partial success
unknown result
checkpoint
~~~

### 阶段四：加入程序性记忆

加入：

~~~text
Skill scope
Skill version
Skill requires_tools
用户确认
中文或多语言触发
Skill 运行审计
~~~

### 阶段五：引入 Graph

只在流程确实需要以下能力时使用 Graph：

~~~text
并行研究
明确路由
检查与修订循环
人类审批
长任务恢复
~~~

普通聊天仍然使用 Loop。

### 阶段六：建立分层 Eval

至少分别测：

~~~text
Gate：该不该检索
Query：搜什么
Retrieval：是否命中
Context：是否注入合适证据
Answer：模型是否正确使用
Action：工具是否真实、幂等、可恢复
Cost：token、延迟和调用次数
~~~

## 18. 一句话总结

Waku 的核心不是某个 memory backend，而是一个清晰的 agent 运行闭环：

~~~text
Harness 管环境和边界
Loop 管思考与行动
Memory 管跨会话信息
Graph 管复杂流程
Eval 管证据和改进
~~~

它最值得学习的设计取舍是：先用可读的本地实现建立正确的运行模型，再通过接口、trace、eval 和可选后端逐步扩展，而不是一开始把所有能力隐藏在大型框架或黑盒 memory 服务中。
