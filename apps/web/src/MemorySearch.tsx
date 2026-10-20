import { uiMessage } from "./ui";
import { useEffect, useRef, useState, type FormEvent } from "react";

type Hit = {
  id: string;
  uri: string;
  tier: string;
  snippet: string;
  score: number;
  evidence_ids: string[];
};
type Result = {
  hits: Hit[];
  context: string;
  estimated_tokens: number;
  truncated: boolean;
  embedding_model: string | null;
  warnings: string[];
  stages: Record<string, unknown>[];
};

export function MemorySearch({
  projectId,
  inspect,
}: {
  projectId: string;
  inspect: (id: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [details, setDetails] = useState(true);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort();
    setResult(null);
    setError("");
    setBusy(false);
    return () => controller.current?.abort();
  }, [projectId]);

  async function search(event: FormEvent) {
    event.preventDefault();
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const params = new URLSearchParams({
        project_id: projectId,
        q: query,
        include_details: String(details),
        max_tokens: "3000",
      });
      const response = await fetch(`/api/memory/search?${params}`, {
        signal: abort.signal,
      });
      const value = await response.json();
      if (!response.ok)
        throw new Error(
          typeof value.detail === "string"
            ? value.detail
            : "请检查检索内容后重试。",
        );
      if (!abort.signal.aborted) setResult(value);
    } catch (e) {
      if (!abort.signal.aborted) setError(uiMessage((e as Error).message));
    } finally {
      if (!abort.signal.aborted) setBusy(false);
    }
  }

  return (
    <section className="panel retrieval-panel">
      <div className="panel-heading">
        <h2>检索项目证据</h2>
        <span>04</span>
      </div>
      <form className="search-form" onSubmit={search}>
        <label htmlFor="memory-query">检索当前项目</label>
        <div className="search-controls">
          <input
            id="memory-query"
            required
            maxLength={4000}
            value={query}
            placeholder="我们对存储方案作了什么决定？"
            onChange={(e) => setQuery(e.target.value)}
          />
          <button disabled={busy}>{busy ? "正在检索……" : "检索证据 ↗"}</button>
        </div>
        <label className="detail-toggle">
          <input
            type="checkbox"
            checked={details}
            onChange={(e) => setDetails(e.target.checked)}
          />
          包含来源详情（L2）
        </label>
      </form>
      {error && (
        <p className="search-error" role="alert">
          {error}
        </p>
      )}
      {result && (
        <div className="search-results">
          <div className="retrieval-status">
            <strong>{result.hits.length} 条证据来源</strong>
            <span>
              {result.estimated_tokens} 上下文预算单位 ·{" "}
              {result.embedding_model ? "关键词与向量混合检索" : "关键词检索"}
            </span>
          </div>
          {result.warnings.map((w) => (
            <p className="retrieval-warning" key={w}>
              {uiMessage(w)}
            </p>
          ))}
          {!result.hits.length && (
            <p className="placeholder">
              当前查询及上下文预算内没有匹配的证据。
            </p>
          )}
          {result.hits.map((hit, i) => (
            <article className="memory-hit" key={hit.id}>
              <div className="hit-heading">
                <span>
                  {String(i + 1).padStart(2, "0")} / {hit.tier.toUpperCase()}
                </span>
                <button onClick={() => inspect(hit.id)}>打开来源 ↗</button>
              </div>
              <code>{hit.uri}</code>
              <p>{hit.snippet}</p>
              <small>
                证据 ID： {hit.evidence_ids.join(", ") || "未关联来源事件"}
              </small>
            </article>
          ))}
          <details>
            <summary>
              组装后的上下文 {result.truncated ? "· 已按预算裁剪" : ""}
            </summary>
            <pre>{result.context || "（空）"}</pre>
          </details>
          <details>
            <summary>检索阶段详情</summary>
            <pre>{JSON.stringify(result.stages, null, 2)}</pre>
          </details>
        </div>
      )}
    </section>
  );
}
