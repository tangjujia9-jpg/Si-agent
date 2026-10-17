import { useEffect, useRef, useState } from 'react';
import { api, post, terminal, type Run, type RunEvent } from './api';

export function RunTrace({ runId, onFinish }: { runId: string; onFinish: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connection, setConnection] = useState('Connecting');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const finish = useRef(onFinish); finish.current = onFinish;
  useEffect(() => {
    let active = true; let source: EventSource | undefined;
    const abort = new AbortController();
    setEvents([]); setRun(null); setError(''); setConnection('Connecting');
    async function connect() {
      let cursor = 0;
      const initial: RunEvent[] = [];
      while (active) {
        const page = await api<RunEvent[]>(`/api/runs/${runId}/events?after=${cursor}`, { signal: abort.signal });
        initial.push(...page); cursor = page.at(-1)?.id ?? cursor;
        if (page.length < 500) break;
      }
      const snapshot = await api<Run>(`/api/runs/${runId}`, { signal: abort.signal });
      if (!active) return;
      setEvents(initial); setRun(snapshot);
      if (terminal(snapshot.status)) { setConnection('Recorded'); finish.current(); return; }
      source = new EventSource(`/api/runs/${runId}/events/stream?after=${cursor}`);
      source.onopen = () => { if (active) setConnection('Live'); };
      source.onerror = () => { if (active) setConnection('Reconnecting · saved events retained'); };
      const receive = (event: Event) => {
        if (!active) return;
        const msg = event as MessageEvent;
        try {
          const id = Number(msg.lastEventId);
          if (!id || id <= cursor) return;
          cursor = id;
          setEvents(rows => [...rows, { id, kind: msg.type, data: JSON.parse(msg.data) }]);
        } catch { setError('A trace event could not be read. Reopen this conversation to reload it.'); }
      };
      ['run_queued', 'run_start', 'span', 'memory_search', 'llm_start', 'llm_end', 'tool_start', 'tool_end',
        'approval_required', 'approval', 'run_end', 'run_pause', 'run_error', 'memory_write', 'decision_start', 'embedding_start']
        .forEach(kind => source!.addEventListener(kind, receive));
      source.addEventListener('snapshot', event => {
        if (!active) return;
        try {
          const next = JSON.parse((event as MessageEvent).data) as Run;
          setRun(next);
          if (terminal(next.status)) { source?.close(); setConnection('Recorded'); finish.current(); }
        } catch { setError('Run snapshot is unavailable.'); }
      });
    }
    void connect().catch(e => { if (active) setError(e.message); });
    return () => { active = false; abort.abort(); source?.close(); };
  }, [runId]);

  async function action(path: string, body = {}) {
    setBusy(true); setError('');
    try { await post(path, body); setRun(await api<Run>(`/api/runs/${runId}`)); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  return <section className="panel trace-panel" aria-label="Run trace">
    <div className="panel-heading"><h2>Run trace</h2><span>{connection}</span></div>
    {error && <p role="alert" className="search-error">{error}</p>}
    {run && <>
      <div className="trace-stats"><div><small>STATUS</small><b className={`status ${run.status}`}>{run.status}</b></div>
        <div><small>MODEL</small><b>{run.model}</b></div><div><small>ITERATIONS</small><b>{run.iterations ?? 0}</b></div>
        <div><small>REPORTED TOKENS</small><b>{run.usage_tokens || 'Not reported'}</b></div></div>
      <p className="provenance">TRACE {run.trace_id} · RESERVED BUDGET {run.charged_tokens ?? 0}</p>
      {run.error_code && <p className="notice">Run stopped: {run.error_code}</p>}
      {!terminal(run.status) && <button className="text-button danger" disabled={busy} onClick={() => void action(`/api/runs/${runId}/cancel`)}>Cancel run</button>}
      {run.operations?.map(op => <article className="operation" key={op.id}>
        <div className="hit-heading"><strong>{op.tool}</strong><span className="status">{op.status}</span></div>
        <details open={op.status === 'pending'}><summary>Operation details</summary><pre>{JSON.stringify(op.arguments, null, 2)}</pre></details>
        {op.status === 'pending' ? <div className="actions"><button disabled={busy} onClick={() => void action(`/api/operations/${op.id}/approval`, { approved: true })}>Approve operation</button>
          <button disabled={busy} className="secondary" onClick={() => void action(`/api/operations/${op.id}/approval`, { approved: false })}>Reject operation</button></div>
          : <details><summary>Recorded result</summary><pre>{JSON.stringify(op.result, null, 2)}</pre></details>}
      </article>)}
    </>}
    <ol className="timeline">{events.map(event => {
      const span = event.kind === 'span';
      return <li key={event.id}><span className="event-number">{String(event.id).padStart(2, '0')}</span>
        <details><summary>{span ? String(event.data.name) : event.kind.replaceAll('_', ' ')}
          {span && <small>{String(event.data.duration_ms)} ms · {String(event.data.status)}</small>}</summary>
          <pre>{JSON.stringify(event.data, null, 2)}</pre></details></li>;
    })}</ol>
  </section>;
}
