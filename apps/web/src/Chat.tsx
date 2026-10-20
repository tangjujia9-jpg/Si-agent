import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import {
  ArrowUp,
  Copy,
  FolderOpen,
  Plus,
  Square,
  Sparkles,
} from "lucide-react";
import {
  api,
  post,
  Citations,
  terminal,
  type Run,
  type Model,
  type Thread,
} from "./api";
import { useRunStream } from "./useRunStream";
import { RunActivity } from "./RunActivity";
import { uiMessage } from "./ui";

type Message = {
  id: string;
  sequence: number;
  role: string;
  content: string;
  event_data: { run_id?: string; redacted?: boolean };
};
export function Chat({
  projectId,
  threadId: chosen,
  onThread,
  open,
  changed,
  onSources,
}: {
  projectId: string;
  threadId?: string;
  onThread?: (thread: Thread) => void;
  open: (uri: string) => void;
  changed: () => void;
  onSources?: () => void;
}) {
  const [ownThread, setOwnThread] = useState("");
  const threadId = chosen ?? ownThread;
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [models, setModels] = useState<Model[]>([]);
  const [modelIndex, setModelIndex] = useState(0);
  const [text, setText] = useState("");
  const [writes, setWrites] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [older, setOlder] = useState(false);
  const [runId, setRunId] = useState("");
  const [copyNotice, setCopyNotice] = useState("");
  const scope = useRef(0),
    bottom = useRef<HTMLDivElement>(null),
    scroll = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const pending = useRef<{
    content: string;
    idempotency_key: string;
    provider: string;
    model: string;
    tools: string[];
    thread: Thread;
  } | null>(null);
  useEffect(
    () => () => {
      scope.current++;
    },
    [],
  );
  useEffect(() => {
    let active = true;
    void api<Model[]>("/api/models")
      .then((catalog) => {
        if (!active) return;
        setModels(catalog);
        const saved = localStorage.getItem("si-model");
        const index = catalog.findIndex(
          (m) => `${m.ref.provider}/${m.ref.model}` === saved,
        );
        setModelIndex(
          index >= 0
            ? index
            : Math.max(
                0,
                catalog.findIndex((m) => m.ref.provider !== "demo"),
              ),
        );
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    if (chosen === undefined)
      void api<Thread[]>(`/api/projects/${projectId}/threads`).then((rows) => {
        if (active)
          setOwnThread(
            rows.find(
              (t) => t.id === localStorage.getItem(`si-thread:${projectId}`),
            )?.id ||
              rows[0]?.id ||
              "",
          );
      });
    return () => {
      active = false;
    };
  }, [projectId, chosen === undefined]);
  const refresh = useCallback(async () => {
    if (!threadId) return;
    const generation = scope.current;
    const [history, records] = await Promise.all([
      api<Message[]>(`/api/threads/${threadId}/messages`),
      api<Run[]>(`/api/threads/${threadId}/runs`),
    ]);
    if (generation !== scope.current) return;
    setMessages(history);
    setRuns(records);
    setOlder(history.length === 100);
    setRunId(records[0]?.id || "");
  }, [threadId]);
  useEffect(() => {
    scope.current++;
    setMessages([]);
    setRuns([]);
    setRunId("");
    setError("");
    setBusy(false);
    pending.current = null;
    follow.current = true;
    if (threadId) localStorage.setItem(`si-thread:${projectId}`, threadId);
    void refresh().catch((e) => setError(e.message));
  }, [projectId, threadId, refresh]);
  const live = useRunStream(runId, () => {
    void refresh().catch((e) => setError(e.message));
    changed();
  });
  const running = live.run
    ? !terminal(live.run.status)
    : runs.some((r) => !terminal(r.status));
  useEffect(() => {
    if (follow.current && bottom.current?.scrollIntoView)
      bottom.current.scrollIntoView({ block: "end" });
  }, [messages, live.text, live.events.length]);

  async function send(event?: FormEvent) {
    event?.preventDefault();
    if (!text.trim() || !models[modelIndex] || running || busy) return;
    setBusy(true);
    setError("");
    const generation = scope.current;
    try {
      let tid = threadId;
      let created: Thread | undefined;
      if (!tid) {
        created =
          pending.current?.thread ||
          (await post<Thread>("/api/threads", {
            project_id: projectId,
            title: text.slice(0, 48),
          }));
        if (generation !== scope.current) return;
        tid = created.id;
      }
      const model = models[modelIndex];
      if (!pending.current || pending.current.content !== text)
        pending.current = {
          content: text,
          idempotency_key: crypto.randomUUID(),
          provider: model.ref.provider,
          model: model.ref.model,
          thread: created || { id: tid, title: "" },
          tools: model.capabilities.tool_calling
            ? [
                "search_memory",
                "read_memory",
                "list_project_files",
                "read_project_file",
                "search_project_files",
                ...(writes
                  ? ["propose_memory", "change_memory", "forget_memory"]
                  : []),
              ]
            : [],
        };
      const { thread: target, ...payload } = pending.current;
      const row = await post<Run>(`/api/threads/${target.id}/messages`, {
        ...payload,
        max_tokens: 64000,
        timeout_seconds: 180,
      });
      if (generation !== scope.current) return;
      setText("");
      pending.current = null;
      setRunId(row.id);
      setRuns((rows) => [row, ...rows]);
      follow.current = true;
      if (created) {
        if (onThread) onThread(created);
        else setOwnThread(created.id);
      }
      if (tid === threadId) await refresh();
    } catch (e) {
      if (generation === scope.current)
        setError(uiMessage((e as Error).message));
    } finally {
      if (generation === scope.current) setBusy(false);
    }
  }
  async function cancel() {
    if (!runId) return;
    try {
      await post(`/api/runs/${runId}/cancel`);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function loadOlder() {
    try {
      const rows = await api<Message[]>(
        `/api/threads/${threadId}/messages?before=${messages[0].sequence}`,
      );
      setMessages((current) => [...rows, ...current]);
      setOlder(rows.length === 100);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const replied = messages.some(
    (m) => m.role === "assistant" && m.event_data.run_id === runId,
  );
  const visible = messages.filter((m) => m.role !== "tool");
  return (
    <section className="conversation" aria-label="项目对话">
      <div
        className="conversation-scroll"
        ref={scroll}
        onScroll={() => {
          const el = scroll.current;
          if (el)
            follow.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        }}
      >
        <div className="conversation-content">
          {!visible.length && !busy && (
            <div className="welcome">
              <div className="si-sigil">
                <Sparkles size={27} strokeWidth={1.4} />
              </div>
              <h1>从项目出发，推进每一步。</h1>
              <p>读懂文件，连接记忆，一起解决问题。</p>
              <div className="suggestions">
                {[
                  "梳理这个项目的整体架构",
                  "回顾项目决策与当前约束",
                  "帮我检查项目中的潜在问题",
                ].map((p) => (
                  <button key={p} onClick={() => setText(p)}>
                    {p}
                    <ArrowUp size={14} />
                  </button>
                ))}
              </div>
              <button className="text-button" onClick={onSources}>
                <FolderOpen size={15} />
                添加项目资料
              </button>
            </div>
          )}
          {older && (
            <button
              className="text-button older"
              onClick={() => void loadOlder()}
            >
              加载更早的消息
            </button>
          )}
          {visible.map((m) => (
            <article key={m.id} className={`message ${m.role}`}>
              {m.role === "user" ? (
                <div className="user-bubble">{m.content}</div>
              ) : (
                <>
                  <div className="assistant-mark">Si</div>
                  {m.event_data.run_id === runId && live.run && (
                    <RunActivity
                      run={live.run}
                      events={live.events}
                      refresh={live.setRun}
                    />
                  )}
                  <div className="message-content markdown">
                    <Citations text={m.content} open={open} />
                  </div>
                  <div className="message-actions">
                    <button
                      aria-label="复制回答"
                      onClick={() =>
                        void navigator.clipboard
                          .writeText(m.content)
                          .then(() => setCopyNotice("已复制"))
                          .catch(() => setCopyNotice("复制失败"))
                      }
                    >
                      <Copy size={13} />
                    </button>
                  </div>
                </>
              )}
            </article>
          ))}
          {runId && !replied && (
            <article className="message assistant">
              <div className="assistant-mark">Si</div>
              {live.run && (
                <RunActivity
                  run={live.run}
                  events={live.events}
                  refresh={live.setRun}
                />
              )}
              <div className="message-content markdown streaming">
                <Citations
                  text={live.text || live.run?.reply || ""}
                  open={open}
                />
                {running && <span className="stream-caret" />}
              </div>
            </article>
          )}
          {busy && <p className="muted">正在发送…</p>}
          {error && (
            <p role="alert" className="notice">
              {error}
            </p>
          )}
          {live.error && <p className="notice">{live.error}</p>}
          {copyNotice && (
            <p role="status" className="muted">
              {copyNotice}
            </p>
          )}
          <div ref={bottom} />
        </div>
      </div>
      <div className="composer-wrap">
        <form className="composer" onSubmit={(e) => void send(e)}>
          <textarea
            id="chat-input"
            aria-label="消息内容"
            rows={2}
            maxLength={4000}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="描述任务，或问一个关于项目的问题…"
            onKeyDown={(e) => {
              if (
                e.key === "Enter" &&
                !e.shiftKey &&
                !e.nativeEvent.isComposing
              ) {
                e.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-bottom">
            <button
              type="button"
              className="icon-button"
              aria-label="添加资料"
              onClick={onSources}
            >
              <Plus size={18} />
            </button>
            <select
              aria-label="模型"
              value={modelIndex}
              onChange={(e) => {
                const i = Number(e.target.value);
                setModelIndex(i);
                localStorage.setItem(
                  "si-model",
                  `${models[i].ref.provider}/${models[i].ref.model}`,
                );
              }}
            >
              {models.map((m, i) => (
                <option key={i} value={i}>
                  {m.ref.model}
                  {m.ref.provider === "demo" ? " · 离线演示" : ""}
                </option>
              ))}
            </select>
            <label className="write-toggle" title="涉及记忆修改时仍需单独审批">
              <input
                type="checkbox"
                checked={writes}
                onChange={(e) => setWrites(e.target.checked)}
              />
              记忆建议
            </label>
            {running ? (
              <button
                type="button"
                className="send"
                aria-label="停止生成"
                onClick={() => void cancel()}
              >
                <Square size={15} fill="currentColor" />
              </button>
            ) : (
              <button
                className="send"
                aria-label="发送消息"
                disabled={busy || !text.trim() || !models.length}
              >
                <ArrowUp size={19} />
              </button>
            )}
          </div>
        </form>
        <div className="composer-hint">
          <span>Enter 发送 · Shift + Enter 换行</span>
          <span>项目文件只读 · 记忆修改需批准</span>
        </div>
      </div>
    </section>
  );
}
