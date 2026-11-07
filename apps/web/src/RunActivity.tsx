import { useState } from "react";
import {
  Check,
  ChevronDown,
  Circle,
  LoaderCircle,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { api, post, terminal, type Run, type RunEvent, type Command } from "./api";
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
  const [logs, setLogs] = useState<Record<string, Command>>({});
  const active = !terminal(run.status);
  const meaningful = events.filter((e) =>
    [
      "memory_search",
      "llm_start",
      "tool_start",
      "tool_end",
      "approval_required",
      "run_error",
      "retrieval_decision",
      "context_recovery",
      "compaction_start",
      "compaction_end",
      "compaction_failed",
      "command_start", "command_end",
    ].includes(e.kind),
  );
  const last = meaningful.at(-1);
  const status =
    run.status === "waiting_approval"
      ? "需要你确认操作"
      : run.commands?.some((c) => ["running", "queued"].includes(c.status))
        ? "正在执行项目命令"
      : last?.kind === "compaction_start"
        ? "正在整理上下文"
      : last?.kind === "tool_start"
        ? `正在${label(String(last.data.tool || "tool"))}`
        : last?.kind === "memory_search"
          ? "正在检索项目资料"
          : "正在生成回答";
  async function approve(id: string, approved: boolean, preview_hash?: string) {
    setBusy(true);
    setError("");
    try {
      await post(`/api/operations/${id}/approval`, { approved, ...(preview_hash ? { preview_hash } : {}) });
      refresh(await api<Run>(`/api/runs/${run.id}`));
    } catch (e) {
      setError(uiMessage((e as Error).message));
    } finally {
      setBusy(false);
    }
  }
  async function readLog(command: Command, more = false) {
    try {
      const previous = logs[command.command_id];
      const offset = more ? previous?.next_offset || 0 : 0;
      const page = await api<Command>(`/api/commands/${command.command_id}?offset=${offset}`);
      setLogs((rows) => ({ ...rows, [command.command_id]: { ...page, output: more ? (previous?.output || "") + page.output : page.output } }));
    } catch (e) { setError((e as Error).message); }
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
              {e.kind === "retrieval_decision"
                ? `检索门 · ${e.data.source === "local-rule" ? "本地规则" : String(e.data.source)} · ${e.data.retrieve ? "检索" : "跳过"}`
                : e.kind === "tool_start" || e.kind === "tool_end"
                ? `${label(String(e.data.tool || "tool"))} · ${e.kind === "tool_start" ? "开始" : "结束"}`
                : e.kind === "llm_start"
                  ? `生成回答 · 第 ${e.data.iteration || 1} 轮`
                  : label(e.kind)}
            </li>
          ))}
        </ol>
      )}
      {run.plan && <div className="task-plan" aria-label="任务计划">
        <small>任务计划</small>
        <ol>{run.plan.steps.map((step, i) => <li key={i} className={step.status}>
          {step.status === "completed" ? <Check size={13} /> : step.status === "in_progress" ? <LoaderCircle size={13} className="spin" /> : <Circle size={13} />}
          <span>{step.title}</span><small>{label(step.status)}</small>
        </li>)}</ol>
      </div>}
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
                  {op.preview?.kind === "patch" ? "确认以下文件修改。批准后会再次检查文件版本。" : op.preview?.kind === "command" ? "确认运行程序。测试结果只对应下面的项目快照。" : "此操作会修改项目记忆，需要你的批准。"}
                </p>
                {op.preview?.kind === "patch" && <div className="patch-preview">{op.preview.files?.map((file) => <details key={file.path} open><summary>{file.path}</summary><pre>{file.diff}</pre></details>)}</div>}
                {op.preview?.kind === "command" && <div className="command-preview">
                  <pre>{op.preview.argv?.map((arg) => JSON.stringify(arg)).join(" ")}</pre>
                  <p>{op.preview.backend === "docker" ? "隔离容器 · 无网络 · 不回写工作区" : "可信本机 · 可访问宿主文件与网络"}</p>
                  <small>目录 {op.preview.cwd} · 上限 {op.preview.timeout_seconds} 秒 · {op.preview.file_count} 个文件</small>
                  <code>快照 {op.preview.snapshot_hash?.slice(0, 16)}</code>
                </div>}
                <details>
                  <summary>查看操作内容</summary>
                  <pre>{JSON.stringify(op.arguments, null, 2)}</pre>
                </details>
                <div className="actions">
                  <button
                    disabled={busy}
                    onClick={() => void approve(op.id, true, op.preview?.hash)}
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
      {run.commands?.map((command) => <div className="command-card" key={command.command_id}>
        <div><b>{label(command.status)}</b><code>{command.argv.join(" ")}</code><span>{command.exit_code != null ? `退出码 ${command.exit_code}` : ""}</span></div>
        <small>快照 {command.snapshot_hash.slice(0, 16)} · {command.backend === "docker" ? "隔离容器" : "可信本机"}</small>
        <div className="actions"><button className="text-button" onClick={() => void readLog(command)}>查看日志</button>
          {["running", "queued"].includes(command.status) && <button className="text-button" onClick={() => void post(`/api/commands/${command.command_id}/cancel`).catch((e) => setError(e.message))}>{command.cancel_requested ? "正在停止" : "停止命令"}</button>}
        </div>
        {logs[command.command_id] && <><pre className="command-log">{logs[command.command_id].output || "暂无输出"}</pre>{logs[command.command_id].next_offset != null && <button className="text-button" onClick={() => void readLog(command, true)}>读取更多</button>}</>}
        {command.error_code && <p className="notice">{label(command.error_code)}</p>}
      </div>)}
      {run.error_code && (
        <p className="notice">
          运行停止：
          {label(run.error_code)}
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
