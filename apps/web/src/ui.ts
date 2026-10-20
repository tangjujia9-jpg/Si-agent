// Translate presentation labels without changing persisted values or API contracts.
const labels: Record<string, string> = {
  queued: "已排队",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  unknown: "结果未知",
  pending: "待审核",
  approved: "已批准",
  rejected: "已拒绝",
  applied: "已应用",
  skipped: "已跳过",
  conflict: "存在冲突",
  waiting_approval: "等待审批",
  waiting_operation: "等待操作完成",
  active: "有效",
  retracted: "已撤回",
  superseded: "已被替代",
  partial: "部分完成",
  expired: "已过期",
  ADD: "新增",
  UPDATE: "更新",
  SUPERSEDES: "替代",
  RETRACT: "撤回",
  SKIP: "跳过",
  directory: "目录",
  resource: "资料",
  semantic: "事实记忆",
  episodic: "事件记忆",
  procedural: "流程记忆",
  session: "会话来源",
  ingest: "资料导入",
  ingestion: "资料导入",
  distill: "会话记忆抽取",
  consolidate: "记忆合并",
  consolidation: "记忆合并",
  index: "索引更新",
  run: "智能体运行",
  job: "后台任务",
  tool_outbox: "工具待执行任务",
  search_memory: "检索记忆",
  read_memory: "读取记忆",
  propose_memory: "提出记忆建议",
  change_memory: "修改记忆",
  forget_memory: "忘记记忆",
  run_queued: "运行已排队",
  run_start: "运行开始",
  run_end: "运行结束",
  run_pause: "运行暂停",
  run_error: "运行错误",
  memory_search: "记忆检索",
  llm_start: "模型调用开始",
  llm_end: "模型调用结束",
  tool_start: "工具开始",
  tool_end: "工具结束",
  approval_required: "请求人工审批",
  approval: "审批结果",
  memory_write: "记忆写入",
  decision_start: "策略判断开始",
  embedding_start: "向量编码开始",
  retrieval_gate: "检索门判断",
  context_compile: "上下文组装",
  tool: "工具执行",
  llm: "模型调用",
  query_embedding: "查询向量编码",
  embedding: "向量编码",
  lexical_search: "关键词检索",
  vector_search: "向量检索",
  rank_fusion: "结果融合",
  list_project_files: "浏览项目文件",
  read_project_file: "读取项目文件",
  search_project_files: "搜索项目文件",
  llm_stream_start: "开始生成",
  llm_delta: "回答片段",
  retrieval_l0_leaves: "L0 摘要召回",
  retrieval_l1_leaves: "L1 概览召回",
  retrieval_l2_leaves: "L2 详情召回",
  retrieval_l0_directories: "L0 目录召回",
  retrieval_l1_directories: "L1 目录召回",
  retrieval_l2_directories: "L2 目录召回",
  "Memory correction": "记忆修正",
};

export const label = (value = "") =>
  labels[value] ?? value.replace(/^Conversation (\d+)$/, "会话 $1");

const messages: Record<string, string> = {
  "Failed to fetch": "无法连接后端，请检查服务是否启动。",
  "NetworkError when attempting to fetch resource.":
    "网络连接失败，请检查后端服务。",
  "Resource not found": "找不到对应资源。",
  "Project not found": "找不到当前项目。",
  "Thread not found": "找不到当前会话。",
  "Job not found": "找不到对应后台任务。",
  "Context node not found": "找不到对应记忆节点。",
  "Candidate not found": "找不到对应候选记忆。",
  "Thread already has an active run":
    "当前会话仍有任务运行，请等待完成或取消后再发送。",
  "Candidate already accepted or resolved":
    "该候选已接受或处理，请刷新查看最新状态。",
  "Correction idempotency conflict":
    "该修正请求已用于其他内容，请刷新后重新提交。",
  "source changed since forget approval":
    "来源已在审批后变化，请刷新并重新预览影响范围。",
  "revision conflict": "记忆版本已变化，请刷新后重试。",
  "Folder unavailable or outside configured workspace roots":
    "无法访问该文件夹，请选择已配置的工作目录。",
  "Linked project folder is unavailable": "关联文件夹已移动或当前不可访问。",
  "File unavailable, excluded or not UTF-8 text":
    "文件不可读、已被过滤或不是 UTF-8 文本。",
  "Embeddings disabled; lexical retrieval was used.":
    "未启用向量编码，本次使用关键词检索。",
  "Embedding unavailable; lexical retrieval was used.":
    "向量编码服务不可用，本次已降级为关键词检索。",
  "Hash demo vectors do not provide semantic paraphrase retrieval.":
    "当前使用哈希演示向量，不具备语义改写检索能力。",
  "No vectors for this model are ready in this scope; request reindex or wait for the worker.":
    "当前项目尚无此模型的可用向量，请等待后台索引任务，或重新建立索引。",
};

export const uiMessage = (value: string) => messages[value] ?? value;

export function nodeTitle(node: { kind: string; title: string; uri: string }) {
  const directories: Record<string, string> = {
    resources: "项目资料",
    decisions: "项目决策",
    tasks: "任务",
    memories: "长期记忆",
    sessions: "会话记录",
    personal: "个人记忆",
    skills: "技能",
  };
  if (node.kind === "directory")
    return (
      directories[node.uri.split("/").at(-1) ?? ""] ??
      (node.title === "Conversation" ? "会话" : node.title)
    );
  return node.kind === "session" && node.title === "User statement"
    ? "用户陈述"
    : node.title;
}
