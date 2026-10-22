import { useState } from "react";
import { api, post, terminal, type RunEvent } from "./api";
import { useRunStream } from "./useRunStream";
import { RunActivity } from "./RunActivity";
import { label } from "./ui";

type WorkingContext = {
  canonical_message_count: number;
  compaction_cursor: number;
  sources_valid: boolean;
  summary: { body?: Record<string, (string | { quote: string; message_id: string })[]>; dependencies?: unknown };
  working_state: unknown;
};
const summaryLabels: Record<string, string> = {
  goals: "任务目标", constraints: "用户原始约束", decisions: "决策与依据",
  progress: "过程记录", open_questions: "未决问题", next_steps: "下一步",
};

export function duration(value: unknown) {
  if (value == null || !Number.isFinite(Number(value))) return "未记录";
  const ms = Number(value);
  return ms < 1
    ? "< 1 ms"
    : ms < 1000
      ? `${ms.toFixed(1)} ms`
      : `${(ms / 1000).toFixed(2)} s`;
}
export function traceLayout(events: RunEvent[]) {
  const spans = events.filter((e) => e.kind === "span");
  const start = (e: RunEvent) => Date.parse(String(e.data.started_at)) || 0;
  const first = Math.min(...spans.map(start));
  const last = Math.max(
    ...spans.map(
      (e) =>
        Date.parse(String(e.data.ended_at)) ||
        start(e) + Number(e.data.duration_ms || 0),
    ),
  );
  const extent = Math.max(1, last - first);
  const byId = new Map(spans.map((e) => [e.data.span_id, e]));
  return spans
    .sort(
      (a, b) =>
        start(a) - start(b) ||
        Number(b.data.duration_ms) - Number(a.data.duration_ms),
    )
    .map((e) => {
      let parent = byId.get(e.data.parent_id),
        depth = 0;
      const seen = new Set([e.data.span_id]);
      while (parent && !seen.has(parent.data.span_id) && depth < 8) {
        seen.add(parent.data.span_id);
        depth++;
        parent = byId.get(parent.data.parent_id);
      }
      return {
        event: e,
        depth,
        offset: Math.max(0, ((start(e) - first) / extent) * 100),
        width: Math.max(0, (Number(e.data.duration_ms || 0) / extent) * 100),
      };
    });
}
export function RunTrace({
  runId,
  onFinish,
}: {
  runId: string;
  onFinish: () => void;
}) {
  const { run, events, connection, error, setRun } = useRunStream(
    runId,
    onFinish,
  );
  const [busy, setBusy] = useState(false);
  const [working, setWorking] = useState<{ runId: string; data: WorkingContext } | null>(null);
  const [contextError, setContextError] = useState("");
  const spans = traceLayout(events);
  const contextPlan = events.filter((e) => ["context_plan", "context_recovery"].includes(e.kind)).at(-1);
  async function cancel() {
    setBusy(true);
    try {
      await post(`/api/runs/${runId}/cancel`);
      setRun(await api(`/api/runs/${runId}`));
    } finally {
      setBusy(false);
    }
  }
  async function inspect() {
    setContextError("");
    try {
      const data = await api<WorkingContext>(`/api/runs/${runId}/context`);
      setWorking({ runId, data });
    } catch {
      setContextError("暂时无法读取工作上下文。");
    }
  }
  const context = working?.runId === runId ? working.data : null;
  return (
    <section className="trace-panel" aria-label="运行轨迹">
      <div className="section-line">
        <h2>运行详情</h2>
        <span className="muted">{connection}</span>
      </div>
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
      {run && (
        <>
          <div className="trace-stats">
            <div>
              <small>状态</small>
              <b>{label(run.status)}</b>
            </div>
            <div>
              <small>模型</small>
              <b>{run.model}</b>
            </div>
            <div>
              <small>迭代</small>
              <b>{run.iterations || 0}</b>
            </div>
            <div>
              <small>模型报告 token</small>
              <b>{run.usage_tokens || "未报告"}</b>
            </div>
          </div>
          <p className="trace-id">Trace {run.trace_id}</p>
          {contextPlan && (
            <details className="raw-events">
              <summary>上下文预算（保守估算）</summary>
              <p>
                输入估算 {String(contextPlan.data.estimated_input_tokens)} · 窗口 {String(contextPlan.data.context_window)}
                {" · "}输出预留 {String(contextPlan.data.max_output_tokens)}
              </p>
              <p>输入采用 UTF-8 保守上界；接口报告的 token 与未结算预留分别统计，摘要不删除原始记录。</p>
              {run.budget_usage && <p>接口报告 {run.budget_usage.reported_tokens} · 未结算预留 {run.budget_usage.unsettled_tokens} · 当前计入预算 {run.budget_usage.charged_tokens}</p>}
              <pre>{JSON.stringify(contextPlan.data, null, 2)}</pre>
            </details>
          )}
          <details className="raw-events" onToggle={(e) => { if (e.currentTarget.open) void inspect(); }}>
            <summary>工作上下文与原文定位</summary>
            {contextError && <p role="alert">{contextError}</p>}
            {context && <>
              <p>原始消息 {context.canonical_message_count} 条 · 已摘要覆盖 {context.compaction_cursor} 条</p>
              {!context.sources_valid ? <p>关联来源已失效，当前摘要不可使用。</p> : <>
                {Object.entries(context.summary.body || {}).map(([key, values]) => (
                  <div key={key}><h4>{summaryLabels[key] || key}</h4><ul>{values.map((item, i) => (
                    <li key={i}>{typeof item === "string" ? item : <>{item.quote} <small>（消息 {item.message_id}）</small></>}</li>
                  ))}</ul></div>
                ))}
                {!context.summary.body && <p>尚未触发语义摘要，仍使用原始交互。</p>}
                <details><summary>执行器任务状态</summary><pre>{JSON.stringify(context.working_state, null, 2)}</pre></details>
                {context.summary.dependencies && <details><summary>摘要原文与证据定位</summary><pre>{JSON.stringify(context.summary.dependencies, null, 2)}</pre></details>}
              </>}
            </>}
          </details>
          <RunActivity run={run} events={events} refresh={setRun} />
          {!terminal(run.status) && (
            <button
              className="secondary"
              disabled={busy}
              onClick={() => void cancel()}
            >
              取消运行
            </button>
          )}
        </>
      )}
      <div className="section-line trace-title">
        <h3>调用耗时</h3>
        <span>相对开始时间 · 高精度实测</span>
      </div>
      <div className="waterfall">
        {spans.map(({ event: e, depth, offset, width }) => (
          <details key={e.id}>
            <summary>
              <span
                className="span-name"
                style={{ paddingLeft: depth * 10 }}
                title={label(String(e.data.name))}
              >
                {label(String(e.data.name))}
              </span>
              <span className="span-bar">
                <i
                  style={{
                    width: `${Math.min(100 - offset, Math.max(0.2, width))}%`,
                    marginLeft: `${offset}%`,
                    background:
                      e.data.status === "failed" ? "#c89a7b" : undefined,
                  }}
                />
              </span>
              <small>{duration(e.data.duration_ms)}</small>
            </summary>
            <pre>{JSON.stringify(e.data, null, 2)}</pre>
          </details>
        ))}
      </div>
      {!spans.length && <p className="muted">运行开始后会记录各阶段耗时。</p>}
      <details className="raw-events">
        <summary>原始事件</summary>
        <ol className="timeline">
          {events
            .filter((e) => !["llm_delta", "span"].includes(e.kind))
            .map((e) => (
              <li key={e.id}>
                <details>
                  <summary>{label(e.kind)}</summary>
                  <pre>{JSON.stringify(e.data, null, 2)}</pre>
                </details>
              </li>
            ))}
        </ol>
      </details>
    </section>
  );
}
