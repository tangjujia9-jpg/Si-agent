import { label, uiMessage } from './ui';
import { useEffect, useRef, useState } from 'react';
import { api, post, terminal, type Run, type RunEvent } from './api';

export function RunTrace({ runId, onFinish }: { runId: string; onFinish: () => void }) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connection, setConnection] = useState('正在连接');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const finish = useRef(onFinish); finish.current = onFinish;
  useEffect(() => {
    let active = true; let source: EventSource | undefined;
    const abort = new AbortController();
    setEvents([]); setRun(null); setError(''); setConnection('正在连接');
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
      if (terminal(snapshot.status)) { setConnection('历史记录'); finish.current(); return; }
      source = new EventSource(`/api/runs/${runId}/events/stream?after=${cursor}`);
      source.onopen = () => { if (active) setConnection('实时更新'); };
      source.onerror = () => { if (active) setConnection('正在重连 · 已保留事件记录'); };
      const receive = (event: Event) => {
        if (!active) return;
        const msg = event as MessageEvent;
        try {
          const id = Number(msg.lastEventId);
          if (!id || id <= cursor) return;
          cursor = id;
          setEvents(rows => [...rows, { id, kind: msg.type, data: JSON.parse(msg.data) }]);
        } catch { setError('无法读取某条轨迹事件，请重新打开会话加载记录。'); }
      };
      ['run_queued', 'run_start', 'span', 'memory_search', 'llm_start', 'llm_end', 'tool_start', 'tool_end',
        'approval_required', 'approval', 'run_end', 'run_pause', 'run_error', 'memory_write', 'decision_start', 'embedding_start']
        .forEach(kind => source!.addEventListener(kind, receive));
      source.addEventListener('snapshot', event => {
        if (!active) return;
        try {
          const next = JSON.parse((event as MessageEvent).data) as Run;
          setRun(next);
          if (terminal(next.status)) { source?.close(); setConnection('历史记录'); finish.current(); }
        } catch { setError('暂时无法读取运行快照。'); }
      });
    }
    void connect().catch(e => { if (active) setError(uiMessage(e.message)); });
    return () => { active = false; abort.abort(); source?.close(); };
  }, [runId]);

  async function action(path: string, body = {}) {
    setBusy(true); setError('');
    try { await post(path, body); setRun(await api<Run>(`/api/runs/${runId}`)); }
    catch (e) { setError(uiMessage((e as Error).message)); }
    finally { setBusy(false); }
  }
  return <section className="panel trace-panel" aria-label="运行轨迹">
    <div className="panel-heading"><h2>运行轨迹</h2><span>{connection}</span></div>
    {error && <p role="alert" className="search-error">{error}</p>}
    {run && <>
      <div className="trace-stats"><div><small>状态</small><b className={`status ${label(run.status)}`}>{label(run.status)}</b></div>
        <div><small>模型</small><b>{run.model}</b></div><div><small>迭代次数</small><b>{run.iterations ?? 0}</b></div>
        <div><small>模型报告的 token</small><b>{run.usage_tokens || '未报告'}</b></div></div>
      <p className="provenance">追踪 ID {run.trace_id} · 已预留 token 预算 {run.charged_tokens ?? 0}</p>
      {run.error_code && <p className="notice">运行停止： {run.error_code}</p>}
      {!terminal(run.status) && <button className="text-button danger" disabled={busy} onClick={() => void action(`/api/runs/${runId}/cancel`)}>取消运行</button>}
      {run.operations?.map(op => <article className="operation" key={op.id}>
        <div className="hit-heading"><strong>{label(op.tool)}</strong><span className="status">{label(op.status)}</span></div>
        <details open={op.status === 'pending'}><summary>操作参数</summary><pre>{JSON.stringify(op.arguments, null, 2)}</pre></details>
        {op.status === 'pending' ? <div className="actions"><button disabled={busy} onClick={() => void action(`/api/operations/${op.id}/approval`, { approved: true })}>批准操作</button>
          <button disabled={busy} className="secondary" onClick={() => void action(`/api/operations/${op.id}/approval`, { approved: false })}>拒绝操作</button></div>
          : <details><summary>执行结果</summary><pre>{JSON.stringify(op.result, null, 2)}</pre></details>}
      </article>)}
    </>}
    <ol className="timeline">{events.map(event => {
      const span = event.kind === 'span';
      return <li key={event.id}><span className="event-number">{String(event.id).padStart(2, '0')}</span>
        <details><summary>{span ? label(String(event.data.name)) : label(event.kind)}
          {span && <small>{String(event.data.duration_ms)} ms · {label(String(event.data.status))}</small>}</summary>
          <pre>{JSON.stringify(event.data, null, 2)}</pre></details></li>;
    })}</ol>
  </section>;
}
