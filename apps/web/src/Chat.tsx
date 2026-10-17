import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { api, post, Citations, terminal, type Run } from './api';
import { RunTrace } from './RunTrace';

type Thread = { id: string; title: string };
type Message = { id: string; sequence: number; role: string; content: string; event_data: { run_id?: string; redacted?: boolean } };
type Model = { ref: { provider: string; model: string }; capabilities: { tool_calling: boolean } };

export function Chat({ projectId, open, changed }: { projectId: string; open: (uri: string) => void; changed: () => void }) {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runId, setRunId] = useState('');
  const [models, setModels] = useState<Model[]>([]);
  const [modelIndex, setModelIndex] = useState(0);
  const [text, setText] = useState('');
  const [writes, setWrites] = useState(false);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [older, setOlder] = useState(false);
  const generation = useRef(0);
  const pending = useRef<{ content: string; key: string; provider: string; model: string; tools: string[] } | null>(null);
  useEffect(() => {
    let active = true;
    Promise.all([api<Thread[]>(`/api/projects/${projectId}/threads`), api<Model[]>('/api/models')])
      .then(([rows, catalog]) => {
        if (!active) return;
        setThreads(rows); setModels(catalog);
        const saved = localStorage.getItem(`si-thread:${projectId}`);
        setThreadId(rows.find(t => t.id === saved)?.id || rows[0]?.id || '');
        setLoading(false);
      }).catch(e => { if (active) { setError(e.message); setLoading(false); } });
    return () => { active = false; };
  }, [projectId]);

  const refresh = useCallback(async () => {
    if (!threadId) return;
    const current = generation.current;
    const [history, records] = await Promise.all([api<Message[]>(`/api/threads/${threadId}/messages`), api<Run[]>(`/api/threads/${threadId}/runs`)]);
    if (current !== generation.current) return;
    setMessages(history); setOlder(history.length === 100); setRuns(records);
  }, [threadId]);
  useEffect(() => {
    generation.current++; setMessages([]); setRuns([]); setRunId(''); setError(''); pending.current = null;
    if (!threadId) return;
    localStorage.setItem(`si-thread:${projectId}`, threadId);
    const current = generation.current;
    void refresh().then(() => api<Run[]>(`/api/threads/${threadId}/runs`)).then(rows => {
      if (current === generation.current) setRunId(rows[0]?.id || '');
    }).catch(e => { if (current === generation.current) setError(e.message); });
  }, [threadId, projectId, refresh]);

  async function createThread() {
    setBusy(true); setError('');
    try {
      const t = await post<Thread>('/api/threads', { project_id: projectId, title: `Conversation ${threads.length + 1}` });
      setThreads(rows => [t, ...rows]); setThreadId(t.id);
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function send(event: FormEvent) {
    event.preventDefault(); if (!threadId || !models[modelIndex]) return;
    setBusy(true); setError('');
    const current = generation.current;
    const model = models[modelIndex];
    const tools = model.capabilities.tool_calling ? ['search_memory', 'read_memory', ...(writes ? ['propose_memory', 'change_memory', 'forget_memory'] : [])] : [];
    if (!pending.current || pending.current.content !== text) pending.current = { content: text, key: crypto.randomUUID(), provider: model.ref.provider, model: model.ref.model, tools };
    const input = pending.current;
    try {
      const row = await post<Run>(`/api/threads/${threadId}/messages`, { ...input, idempotency_key: input.key, key: undefined });
      if (current !== generation.current) return;
      setText(''); pending.current = null; setRunId(row.id); await refresh();
    } catch (e) { if (current === generation.current) setError((e as Error).message); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function distill() {
    setBusy(true); setError('');
    try { await post(`/api/threads/${threadId}/memory/distill`); changed(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function loadOlder() {
    try {
      const rows = await api<Message[]>(`/api/threads/${threadId}/messages?before=${messages[0].sequence}`);
      setMessages(existing => [...rows, ...existing]); setOlder(rows.length === 100);
    } catch (e) { setError((e as Error).message); }
  }
  const running = runs.some(r => !terminal(r.status));
  return <div className="chat-layout">
    <section className="panel chat-panel">
      <div className="panel-heading"><h2>Project conversation</h2><button className="text-button" disabled={busy || loading} onClick={() => void createThread()}>New conversation +</button></div>
      <div className="chat-toolbar"><label>CONVERSATION<select value={threadId} onChange={e => setThreadId(e.target.value)} disabled={busy}>
        <option value="" disabled>Select a conversation</option>{threads.map(t => <option value={t.id} key={t.id}>{t.title}</option>)}</select></label>
        <label>MODEL<select value={modelIndex} onChange={e => setModelIndex(Number(e.target.value))} disabled={busy}>
          {models.map((m, i) => <option key={i} value={i}>{m.ref.provider} / {m.ref.model}</option>)}</select></label></div>
      {error && <p role="alert" className="notice">{error}</p>}
      {error && pending.current && <p className="notice">Retry preserves the original model and tool selection. Edit the message to start a different request.</p>}
      <div className="messages" aria-label="Conversation history">
        {older && <button className="text-button" onClick={() => void loadOlder()}>Load older messages</button>}
        {!messages.length && <div className="chat-empty"><span className="eyebrow">FROM SOURCES TO DECISIONS</span><h2>Work with what your project knows.</h2><p>Ask about a decision, inspect its evidence, or propose a correction. Start a conversation to begin.</p></div>}
        {messages.map(m => <article key={m.id} className={`message ${m.role}`}><div className="message-label">{m.role === 'user' ? 'YOU' : 'SI'}<small>#{m.sequence}</small></div>
          <div className="message-content"><Citations text={m.content} open={open} /></div></article>)}
        {running && <p role="status" className="working">Si is working · follow the run trace</p>}
      </div>
      <form className="composer" onSubmit={send}><label htmlFor="chat-input">MESSAGE</label><textarea id="chat-input" required maxLength={4000} value={text} onChange={e => setText(e.target.value)} placeholder="What did we decide about project storage?" />
        <label className="detail-toggle"><input type="checkbox" checked={writes} onChange={e => setWrites(e.target.checked)} />Enable memory changes · approval required</label>
        <div className="actions"><button type="submit" disabled={!threadId || busy || running || !models.length}>{busy ? 'Sending…' : 'Send message ↗'}</button>
          <button className="secondary" type="button" disabled={!threadId || busy} onClick={() => void distill()}>Extract memory now</button></div>
        <p className="fine-print">The offline model demonstrates evidence retrieval. Natural language extraction requires a configured model. SSE streams progress and the completed answer.</p>
      </form>
    </section>
    <div className="trace-column">{runs.length > 0 && <label>RUN HISTORY<select value={runId} onChange={e => setRunId(e.target.value)}>{runs.map(r => <option key={r.id} value={r.id}>{r.id.slice(0, 8)} · {r.status}</option>)}</select></label>}
      {runId ? <RunTrace runId={runId} onFinish={() => { void refresh().catch(e => setError(e.message)); changed(); }} />
        : <section className="panel"><div className="panel-heading"><h2>Run trace</h2><span>WAITING</span></div><p className="placeholder">Retrieval, model calls and tool approvals appear here when a run starts.</p></section>}</div>
  </div>;
}
