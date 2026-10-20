import { useState } from "react";
import {
  Check,
  ChevronDown,
  Circle,
  LoaderCircle,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { api, post, terminal, type Run, type RunEvent } from "./api";
import { label, uiMessage } from "./ui";

export function RunActivity({
  run,
  events,
  refresh,
}: {
  run: Run;
  events: RunEvent[];
  refresh: (run: Run) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const active = !terminal(run.status);
  const meaningful = events.filter((e) =>
    [
      "memory_search",
      "llm_start",
      "tool_start",
      "tool_end",
      "approval_required",
      "run_error",
    ].includes(e.kind),
  );
  const last = meaningful.at(-1);
  const status =
    run.status === "waiting_approval"
      ? "需要你确认操作"
      : last?.kind === "tool_start"
        ? `正在${label(String(last.data.tool || "tool"))}`
        : last?.kind === "memory_search"
          ? "正在检索项目资料"
          : "正在生成回答";
  async function approve(id: string, approved: boolean) {
    setBusy(true);
    setError("");
    try {
      await post(`/api/operations/${id}/approval`, { approved });
      refresh(await api<Run>(`/api/runs/${run.id}`));
    } catch (e) {
      setError(uiMessage((e as Error).message));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="run-activity" aria-label="执行过程">
      <button
        className="activity-heading"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {active ? (
          <LoaderCircle size={14} className="spin" />
        ) : run.status === "succeeded" ? (
          <Check size={14} />
        ) : (
          <Circle size={14} />
        )}
        <span>
          {active
            ? status
            : run.status === "succeeded"
              ? `已完成 · ${run.iterations || 1} 轮处理`
              : label(run.status)}
        </span>
        <ChevronDown size={13} className={expanded ? "rotated" : ""} />
      </button>
      {(expanded || active) && (
        <ol className="activity-steps">
          {meaningful.slice(-12).map((e) => (
            <li key={e.id}>
              <span className="step-dot" />
              {e.kind === "tool_start" || e.kind === "tool_end"
                ? `${label(String(e.data.tool || "tool"))} · ${e.kind === "tool_start" ? "开始" : "结束"}`
                : e.kind === "llm_start"
                  ? `生成回答 · 第 ${e.data.iteration || 1} 轮`
                  : label(e.kind)}
            </li>
          ))}
        </ol>
      )}
      {(expanded || active) &&
        run.operations?.map((op) => (
          <div className={`tool-card ${op.status}`} key={op.id}>
            <div>
              <Wrench size={14} />
              <b>{label(op.tool)}</b>
              <span>{label(op.status)}</span>
            </div>
            {op.arguments.path != null && (
              <code>{String(op.arguments.path)}</code>
            )}
            {op.arguments.query != null && <p>{String(op.arguments.query)}</p>}
            {op.status === "pending" && (
              <>
                <p>
                  <ShieldCheck size={14} />
                  此操作会修改项目记忆，需要你的批准。
                </p>
                <details>
                  <summary>查看操作内容</summary>
                  <pre>{JSON.stringify(op.arguments, null, 2)}</pre>
                </details>
                <div className="actions">
                  <button
                    disabled={busy}
                    onClick={() => void approve(op.id, true)}
                  >
                    批准操作
                  </button>
                  <button
                    className="secondary"
                    disabled={busy}
                    onClick={() => void approve(op.id, false)}
                  >
                    拒绝操作
                  </button>
                </div>
              </>
            )}
          </div>
        ))}
      {run.error_code && (
        <p className="notice">
          运行停止：
          {run.error_code === "context_budget"
            ? "上下文预算不足，请缩小任务或新建会话。"
            : run.error_code}
        </p>
      )}
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
    </div>
  );
}
