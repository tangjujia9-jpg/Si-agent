import { useEffect, useRef, useState } from "react";
import { Check, ChevronRight, History, Sparkles, X } from "lucide-react";
import {
  api,
  post,
  type Candidate,
  type ProjectNode,
  type Evidence,
} from "./api";
import { label, uiMessage } from "./ui";

export function MemoryReview({
  projectId,
  nodes,
  changed,
  open,
}: {
  projectId: string;
  nodes: ProjectNode[];
  changed: () => void;
  open: (id: string) => void;
}) {
  const [rows, setRows] = useState<Candidate[]>([]);
  const [history, setHistory] = useState(false);
  const [offset, setOffset] = useState(0);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [quotes, setQuotes] = useState<Record<string, Evidence[]>>({});
  const [filter, setFilter] = useState("");
  const [view, setView] = useState("saved");
  const previous = useRef("");
  const notify = useRef(changed);
  notify.current = changed;
  useEffect(() => {
    let active = true;
    const abort = new AbortController();
    const load = () =>
      api<Candidate[]>(
        `/api/projects/${projectId}/memory/candidates?limit=30&offset=${offset}${history ? "" : "&status=pending,conflict"}`,
        { signal: abort.signal },
      )
        .then((data) => {
          if (active) {
            const signature = JSON.stringify(
              data.map((c) => [c.id, c.status, c.job_id]),
            );
            if (previous.current && previous.current !== signature)
              notify.current();
            previous.current = signature;
            setRows(data);
            setMore(data.length === 30);
          }
        })
        .catch((e) => {
          if (active) setError(e.message);
        });
    void load();
    const timer = setInterval(() => void load(), 2500);
    return () => {
      active = false;
      abort.abort();
      clearInterval(timer);
    };
  }, [projectId, busy, history, offset]);
  async function decide(c: Candidate, action: string) {
    setBusy(c.id);
    setError("");
    try {
      await post(`/api/memory/candidates/${c.id}/${action}`);
      setRows((current) => current.filter((row) => row.id !== c.id));
      changed();
    } catch (e) {
      setError(uiMessage((e as Error).message));
    } finally {
      setBusy("");
    }
  }
  async function evidence(c: Candidate) {
    try {
      const data = await api<Evidence[]>(
        `/api/projects/${projectId}/evidence?ids=${c.evidence_ids.join(",")}`,
      );
      setQuotes((current) => ({ ...current, [c.id]: data }));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  const memories = nodes.filter(
    (n) =>
      ["semantic", "episodic", "procedural"].includes(n.kind) &&
      `${n.title} ${n.abstract}`.toLowerCase().includes(filter.toLowerCase()),
  );
  return (
    <div className="memory-view">
      <div className="segmented">
        <button
          className={view === "saved" ? "active" : ""}
          onClick={() => setView("saved")}
        >
          已记住
        </button>
        <button
          className={view === "suggested" ? "active" : ""}
          onClick={() => setView("suggested")}
        >
          待确认建议
          {rows.filter((c) => c.status === "pending" && !c.job_id).length >
            0 && (
            <span className="count-badge">
              {rows.filter((c) => c.status === "pending" && !c.job_id).length}
            </span>
          )}
        </button>
      </div>
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
      {view === "saved" ? (
        <>
          <input
            className="filter-input"
            aria-label="搜索记忆"
            placeholder="搜索已记住的事实、决策与规范…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {!memories.length && (
            <div className="quiet-empty">
              <Sparkles size={26} strokeWidth={1.3} />
              <p>值得保留的项目事实会积累在这里。</p>
            </div>
          )}
          {memories.map((n) => (
            <button
              className="saved-memory"
              key={n.id}
              onClick={() => open(n.id)}
            >
              <span>{n.abstract || n.title}</span>
              <small>
                {n.needs_review ? "来源变化 · 待确认" : label(n.kind)}
              </small>
              <ChevronRight size={14} />
            </button>
          ))}
        </>
      ) : (
        <>
          <div className="section-line">
            <p className="muted">确认后，Si 会在今后的对话中记住这些内容。</p>
            <button
              className="icon-button"
              aria-label={history ? "只看待确认" : "查看建议历史"}
              onClick={() => {
                setHistory((v) => !v);
                setOffset(0);
              }}
            >
              <History size={16} />
            </button>
          </div>
          {!rows.length && (
            <div className="quiet-empty">
              <Check size={26} strokeWidth={1.3} />
              <p>目前没有需要确认的建议。</p>
            </div>
          )}
          {rows.map((c) => {
            const current = nodes.find((n) => n.uri === c.uri);
            return (
              <article className="candidate" key={c.id}>
                <div className="section-line">
                  <span className="muted">
                    {label(c.action)}
                    {history ? ` · ${label(c.status)}` : ""}
                  </span>
                  {c.job_id && c.status === "pending" && (
                    <span className="muted">正在保存</span>
                  )}
                </div>
                <p className="candidate-content">
                  {c.content || "撤回此条记忆"}
                </p>
                {c.action !== "ADD" && current && (
                  <details>
                    <summary>对比已保存内容</summary>
                    <p>{current.abstract || current.title}</p>
                  </details>
                )}
                <button
                  className="text-button"
                  onClick={() => void evidence(c)}
                >
                  查看依据
                </button>
                {c.status === "conflict" && (
                  <div className="notice">
                    <p>这条建议尚未保存：来源或内容版本已变化。</p>
                    {current && (
                      <button
                        className="text-button"
                        onClick={() => open(current.id)}
                      >
                        核对当前记忆
                      </button>
                    )}
                    <button
                      className="text-button"
                      disabled={!!busy}
                      onClick={() => void decide(c, "skip")}
                    >
                      忽略此建议
                    </button>
                  </div>
                )}
                {quotes[c.id]?.map((e) => (
                  <blockquote key={e.id}>
                    {e.quote || "[已忘记]"}
                    <button
                      className="text-button"
                      onClick={() => open(e.node_id)}
                    >
                      打开来源
                    </button>
                  </blockquote>
                ))}
                {c.status === "pending" && !c.job_id && (
                  <div className="actions">
                    <button
                      disabled={!!busy}
                      onClick={() => void decide(c, "apply")}
                    >
                      <Check size={14} />
                      保存这条记忆
                    </button>
                    <button
                      className="secondary"
                      disabled={!!busy}
                      onClick={() => void decide(c, "skip")}
                    >
                      <X size={14} />
                      忽略
                    </button>
                  </div>
                )}
                {history && c.node_id && (
                  <button
                    className="text-button"
                    onClick={() => open(c.node_id!)}
                  >
                    查看已保存记忆
                  </button>
                )}
              </article>
            );
          })}
          <div className="pagination">
            <button
              className="text-button"
              disabled={!offset}
              onClick={() => setOffset((n) => Math.max(0, n - 30))}
            >
              上一页
            </button>
            <button
              className="text-button"
              disabled={!more}
              onClick={() => setOffset((n) => n + 30)}
            >
              下一页
            </button>
          </div>
        </>
      )}
    </div>
  );
}
