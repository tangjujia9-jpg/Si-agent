import { useState } from "react";
import { api, post, terminal, type RunEvent } from "./api";
import { useRunStream } from "./useRunStream";
import { RunActivity } from "./RunActivity";
import { label } from "./ui";

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
              <p>按 UTF-8 字节估算上界，实际模型 token 见用量；收缩不删除原始记录。</p>
              <pre>{JSON.stringify(contextPlan.data, null, 2)}</pre>
            </details>
          )}
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
