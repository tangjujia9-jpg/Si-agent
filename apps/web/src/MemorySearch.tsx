import { useEffect, useRef, useState, type FormEvent } from 'react';

type Hit = { id: string; uri: string; tier: string; snippet: string; score: number; evidence_ids: string[] };
type Result = { hits: Hit[]; context: string; estimated_tokens: number; truncated: boolean;
  embedding_model: string | null; warnings: string[]; stages: Record<string, unknown>[] };

export function MemorySearch({ projectId, inspect }: { projectId: string; inspect: (id: string) => void }) {
  const [query, setQuery] = useState('');
  const [details, setDetails] = useState(true);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    controller.current?.abort(); setResult(null); setError(''); setBusy(false);
    return () => controller.current?.abort();
  }, [projectId]);

  async function search(event: FormEvent) {
    event.preventDefault(); controller.current?.abort();
    const abort = new AbortController(); controller.current = abort;
    setBusy(true); setError(''); setResult(null);
    try {
      const params = new URLSearchParams({ project_id: projectId, q: query, include_details: String(details), max_tokens: '3000' });
      const response = await fetch(`/api/memory/search?${params}`, { signal: abort.signal });
      const value = await response.json();
      if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Check the search query.');
      if (!abort.signal.aborted) setResult(value);
    } catch (e) { if (!abort.signal.aborted) setError((e as Error).message); }
    finally { if (!abort.signal.aborted) setBusy(false); }
  }

  return <section className="panel retrieval-panel">
    <div className="panel-heading"><h2>Find project evidence</h2><span>04</span></div>
    <form className="search-form" onSubmit={search}>
      <label htmlFor="memory-query">SEARCH THIS PROJECT</label>
      <div className="search-controls"><input id="memory-query" required maxLength={4000} value={query}
        placeholder="What did we decide about storage?" onChange={e => setQuery(e.target.value)} />
        <button disabled={busy}>{busy ? 'Searching…' : 'Search evidence ↗'}</button></div>
      <label className="detail-toggle"><input type="checkbox" checked={details} onChange={e => setDetails(e.target.checked)} />Include source detail (L2)</label>
    </form>
    {error && <p className="search-error" role="alert">{error}</p>}
    {result && <div className="search-results">
      <div className="retrieval-status"><strong>{result.hits.length} evidence sources</strong><span>{result.estimated_tokens} budget units · {result.embedding_model ? 'Hybrid search' : 'Lexical search'}</span></div>
      {result.warnings.map(w => <p className="retrieval-warning" key={w}>{w}</p>)}
      {!result.hits.length && <p className="placeholder">No evidence fits this query and context budget.</p>}
      {result.hits.map((hit, i) => <article className="memory-hit" key={hit.id}>
        <div className="hit-heading"><span>{String(i + 1).padStart(2, '0')} / {hit.tier.toUpperCase()}</span>
          <button onClick={() => inspect(hit.id)}>Open source ↗</button></div>
        <code>{hit.uri}</code><p>{hit.snippet}</p>
        <small>Evidence: {hit.evidence_ids.join(', ') || 'No source event attached'}</small>
      </article>)}
      <details><summary>Compiled context {result.truncated ? '· trimmed to budget' : ''}</summary><pre>{result.context || '(empty)'}</pre></details>
      <details><summary>Retrieval stages</summary><pre>{JSON.stringify(result.stages, null, 2)}</pre></details>
    </div>}
  </section>;
}
