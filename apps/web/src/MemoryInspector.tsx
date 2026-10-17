import { label, nodeTitle, uiMessage } from './ui';
import { useEffect, useState, type FormEvent } from 'react';
import { api, post, type ProjectNode, type Evidence, type Candidate } from './api';

type Version = { revision: number; snapshot: { content: string; status: string }; created_at: string };
type Preview = { derived_node_ids: string[]; redacts_messages: boolean; message: string };

export function MemoryInspector({ nodeId, projectId, revision, changed, open }: {
  nodeId: string; projectId: string; revision: number; changed: () => void; open: (id: string) => void;
}) {
  const [node, setNode] = useState<ProjectNode | null>(null);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [support, setSupport] = useState<Evidence[]>([]);
  const [versions, setVersions] = useState<Version[]>([]);
  const [tier, setTier] = useState('l2');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [correction, setCorrection] = useState('');
  const [preview, setPreview] = useState<Preview | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [compare, setCompare] = useState('');
  useEffect(() => {
    const abort = new AbortController(); const init = { signal: abort.signal };
    setError(''); setPreview(null); setConfirmed(false); setNotice(''); setNode(null);
    Promise.all([api<ProjectNode>(`/api/memory/${nodeId}`, init), api<Evidence[]>(`/api/memory/${nodeId}/evidence`, init),
      api<Version[]>(`/api/memory/${nodeId}/versions`, init)])
      .then(async ([n, own, history]) => {
        const refs = n.source_event_ids.length ? await api<Evidence[]>(`/api/projects/${projectId}/evidence?${new URLSearchParams({ ids: n.source_event_ids.join(',') })}`, init) : [];
        if (abort.signal.aborted) return;
        setNode(n); setEvidence(own); setSupport(refs); setVersions(history); setCorrection(n.content || '');
        setCompare(String(history.find(v => v.revision !== n.revision)?.revision || ''));
      }).catch(e => { if (!abort.signal.aborted) setError(uiMessage(e.message)); });
    return () => abort.abort();
  }, [nodeId, projectId, revision]);

  async function submitCorrection(event: FormEvent) {
    event.preventDefault(); if (!node) return;
    setBusy(true); setError('');
    try {
      await post<Candidate>(`/api/memory/${node.id}/corrections`, { content: correction, expected_revision: node.revision, idempotency_key: crypto.randomUUID() });
      setNotice('修正已保存为待审核候选，请到“记忆审核”中确认接受。'); changed();
    } catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(false); }
  }
  async function retract() {
    if (!node) return;
    setBusy(true); setError('');
    try {
      await post(`/api/projects/${projectId}/memory/candidates`, { uri: node.uri, title: node.title, kind: node.kind,
        action: 'RETRACT', content: '', expected_revision: node.revision, evidence_ids: evidence.filter(e => !e.redacted).map(e => e.id), idempotency_key: crypto.randomUUID() });
      setNotice('撤回已提交处理，历史仍会保留。若需脱敏正文和历史，请使用忘记操作。'); changed();
    } catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(false); }
  }
  async function forget() {
    if (!node || !confirmed) return;
    setBusy(true); setError('');
    try { await post(`/api/memory/${node.id}/forget`, { reason: 'user_request_from_inspector', expected_revision: node.revision }); changed(); }
    catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(false); }
  }
  const historical = versions.find(v => String(v.revision) === compare);
  return <section className="panel evidence-panel" aria-label="记忆检查器">
    <div className="panel-heading"><h2>记忆检查器</h2><span>正文 · 证据 · 历史</span></div>
    {error && <p className="notice" role="alert">{error}</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {!node ? <p className="placeholder">正在加载来源……</p> : <>
      <div className="source-meta"><h3>{nodeTitle(node)}</h3><span>{label(node.kind)} / 正文 v{node.revision} / 摘要表示 v{node.representation_revision}</span></div>
      <code className="uri">{node.uri}</code><p className="provenance">{label(node.status)}{node.needs_review ? ' · 待复核 · 修正前不参与检索' : ''}</p>
      <div className="tier-tabs" aria-label="上下文层级">{['l0', 'l1', 'l2'].map(t => <button key={t} className={tier === t ? 'active' : ''} aria-pressed={tier === t} onClick={() => setTier(t)}>{t.toUpperCase()} / {t === 'l0' ? '摘要' : t === 'l1' ? '概览' : '详情'}</button>)}</div>
      <pre>{(tier === 'l0' ? node.abstract : tier === 'l1' ? node.overview : node.content) || (node.kind === 'directory' ? '此目录用于组织项目上下文。' : '[内容为空或已忘记]')}</pre>
      {node.kind !== 'directory' && <div className="inspector-body">
        <details open><summary>来源证据 · {support.length || evidence.length}</summary>
          {(node.status !== 'retracted' && support.length ? support : evidence).map(e => <article key={e.id} className="evidence-quote"><small>v{e.revision} · 字符位置 {e.start_char}–{e.end_char} · {e.redacted ? '已脱敏' : e.id}</small>
            <blockquote>{e.quote || '[已忘记]'}</blockquote><button className="text-button" onClick={() => open(e.node_id)}>打开原始来源 ↗</button></article>)}</details>
        <details><summary>版本对比 · {versions.length} 个版本</summary>
          <label>对比版本<select value={compare} onChange={e => setCompare(e.target.value)}><option value="">请选择版本</option>{versions.map(v => <option key={v.revision} value={v.revision}>版本 {v.revision} · {label(v.snapshot.status)}</option>)}</select></label>
          {historical && <div className="version-columns"><div><small>历史版本 {historical.revision}</small><pre>{historical.snapshot.content || '[内容为空或已忘记]'}</pre></div><div><small>当前版本 v{node.revision}</small><pre>{node.content || '[内容为空或已忘记]'}</pre></div></div>}</details>
        {node.status === 'active' && ['semantic', 'episodic', 'procedural'].includes(node.kind) && <details><summary>修正这条记忆</summary>
          <p className="fine-print">你的新陈述会成为独立来源；候选审核通过后，当前事实才会更新。</p>
          <form onSubmit={submitCorrection}><label htmlFor="correction">新的事实陈述</label><textarea id="correction" value={correction} onChange={e => setCorrection(e.target.value)} maxLength={4000} required />
            <div className="actions"><button disabled={busy || !correction.trim()}>提交修正候选</button><button type="button" className="secondary danger" disabled={busy || !evidence.length} onClick={() => void retract()}>撤回事实</button></div></form></details>}
        {node.status !== 'retracted' && <details><summary className="danger">忘记此来源</summary><p className="fine-print">忘记会脱敏已存储的来源历史及已知依赖项，无法撤销已经发送给外部模型的请求。</p>
          <button className="secondary" disabled={busy} onClick={() => { void api<Preview>(`/api/memory/${node.id}/forget-preview`).then(setPreview).catch(e => setError(uiMessage(e.message))); }}>预览影响范围</button>
          {preview && <div className="forget-preview"><p>{preview.derived_node_ids.length} 条派生记忆也将被脱敏。{preview.redacts_messages && '原始用户消息也将被脱敏。'}</p>
            {preview.derived_node_ids.map(id => <button key={id} className="text-button" onClick={() => open(id)}>{id}</button>)}
            <label className="detail-toggle"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我已知晓来源历史将被脱敏</label>
            <button className="danger secondary" disabled={busy || !confirmed} onClick={() => void forget()}>确认忘记</button></div>}</details>}
      </div>}
    </>}
  </section>;
}
