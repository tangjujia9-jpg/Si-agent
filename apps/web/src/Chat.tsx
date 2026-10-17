import { label, uiMessage } from './ui';
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
      }).catch(e => { if (active) { setError(uiMessage(e.message)); setLoading(false); } });
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
    }).catch(e => { if (current === generation.current) setError(uiMessage(e.message)); });
  }, [threadId, projectId, refresh]);

  async function createThread() {
    setBusy(true); setError('');
    try {
      const t = await post<Thread>('/api/threads', { project_id: projectId, title: `会话 ${threads.length + 1}` });
      setThreads(rows => [t, ...rows]); setThreadId(t.id);
    } catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(false); }
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
    } catch (e) { if (current === generation.current) setError(uiMessage((e as Error).message)); }
    finally { if (current === generation.current) setBusy(false); }
  }
  async function distill() {
    setBusy(true); setError('');
    try { await post(`/api/threads/${threadId}/memory/distill`); changed(); }
    catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(false); }
  }
  async function loadOlder() {
    try {
      const rows = await api<Message[]>(`/api/threads/${threadId}/messages?before=${messages[0].sequence}`);
      setMessages(existing => [...rows, ...existing]); setOlder(rows.length === 100);
    } catch (e) { setError(uiMessage((e as Error).message)); }
  }
  const running = runs.some(r => !terminal(r.status));
  return <div className="chat-layout">
    <section className="panel chat-panel">
      <div className="panel-heading"><h2>项目对话</h2><button className="text-button" disabled={busy || loading} onClick={() => void createThread()}>新建会话 +</button></div>
      <div className="chat-toolbar"><label>当前会话<select value={threadId} onChange={e => setThreadId(e.target.value)} disabled={busy}>
        <option value="" disabled>请选择会话</option>{threads.map(t => <option value={t.id} key={t.id}>{label(t.title)}</option>)}</select></label>
        <label>模型<select value={modelIndex} onChange={e => setModelIndex(Number(e.target.value))} disabled={busy}>
          {models.map((m, i) => <option key={i} value={i}>{m.ref.provider} / {m.ref.model}</option>)}</select></label></div>
      {error && <p role="alert" className="notice">{error}</p>}
      {error && pending.current && <p className="notice">重试将沿用原模型、工具和请求标识；修改消息后会创建新请求。</p>}
      <div className="messages" aria-label="对话历史">
        {older && <button className="text-button" onClick={() => void loadOlder()}>加载更早的消息</button>}
        {!messages.length && <div className="chat-empty"><span className="eyebrow">从资料到决策</span><h2>让项目知识参与每次决策。</h2><p>新建会话后，可以询问项目决策、检查依据，或提出记忆修正。</p></div>}
        {messages.map(m => <article key={m.id} className={`message ${m.role}`}><div className="message-label">{m.role === 'user' ? '你' : 'SI'}<small>#{m.sequence}</small></div>
          <div className="message-content"><Citations text={m.content} open={open} /></div></article>)}
        {running && <p role="status" className="working">Si 正在处理，请查看右侧运行轨迹</p>}
      </div>
      <form className="composer" onSubmit={send}><label htmlFor="chat-input">消息内容</label><textarea id="chat-input" required maxLength={4000} value={text} onChange={e => setText(e.target.value)} placeholder="我们之前对项目存储方案作了什么决定？" />
        <label className="detail-toggle"><input type="checkbox" checked={writes} onChange={e => setWrites(e.target.checked)} />允许智能体修改记忆（工具执行前需审批）</label>
        <div className="actions"><button type="submit" disabled={!threadId || busy || running || !models.length}>{busy ? '正在发送……' : '发送消息 ↗'}</button>
          <button className="secondary" type="button" disabled={!threadId || busy} onClick={() => void distill()}>立即抽取记忆</button></div>
        <p className="fine-print">离线模型用于演示证据检索；自然语言记忆抽取需要配置模型。页面实时展示运行进度，并在完成后显示回答。</p>
      </form>
    </section>
    <div className="trace-column">{runs.length > 0 && <label>运行历史<select value={runId} onChange={e => setRunId(e.target.value)}>{runs.map(r => <option key={r.id} value={r.id}>{r.id.slice(0, 8)} · {label(r.status)}</option>)}</select></label>}
      {runId ? <RunTrace runId={runId} onFinish={() => { void refresh().catch(e => setError(uiMessage(e.message))); changed(); }} />
        : <section className="panel"><div className="panel-heading"><h2>运行轨迹</h2><span>等待运行</span></div><p className="placeholder">开始对话后，这里会显示检索、模型调用、工具执行和审批记录。</p></section>}</div>
  </div>;
}
