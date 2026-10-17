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
      }).catch(e => { if (!abort.signal.aborted) setError(e.message); });
    return () => abort.abort();
  }, [nodeId, projectId, revision]);

  async function submitCorrection(event: FormEvent) {
    event.preventDefault(); if (!node) return;
    setBusy(true); setError('');
    try {
      await post<Candidate>(`/api/memory/${node.id}/corrections`, { content: correction, expected_revision: node.revision, idempotency_key: crypto.randomUUID() });
      setNotice('Correction saved as a reviewable candidate. Open Memory review to accept it.'); changed();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function retract() {
    if (!node) return;
    setBusy(true); setError('');
    try {
      await post(`/api/projects/${projectId}/memory/candidates`, { uri: node.uri, title: node.title, kind: node.kind,
        action: 'RETRACT', content: '', expected_revision: node.revision, evidence_ids: evidence.filter(e => !e.redacted).map(e => e.id), idempotency_key: crypto.randomUUID() });
      setNotice('Retraction queued. History remains available; this is different from forgetting.'); changed();
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  async function forget() {
    if (!node || !confirmed) return;
    setBusy(true); setError('');
    try { await post(`/api/memory/${node.id}/forget`, { reason: 'user_request_from_inspector', expected_revision: node.revision }); changed(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }
  const historical = versions.find(v => String(v.revision) === compare);
  return <section className="panel evidence-panel" aria-label="Memory inspector">
    <div className="panel-heading"><h2>Memory inspector</h2><span>CONTENT · EVIDENCE · HISTORY</span></div>
    {error && <p className="notice" role="alert">{error}</p>}
    {notice && <p className="notice" role="status">{notice}</p>}
    {!node ? <p className="placeholder">Loading source…</p> : <>
      <div className="source-meta"><h3>{node.title}</h3><span>{node.kind} / content v{node.revision} / representation v{node.representation_revision}</span></div>
      <code className="uri">{node.uri}</code><p className="provenance">{node.status}{node.needs_review ? ' · NEEDS REVIEW · excluded from retrieval until corrected' : ''}</p>
      <div className="tier-tabs" aria-label="Context level">{['l0', 'l1', 'l2'].map(t => <button key={t} className={tier === t ? 'active' : ''} aria-pressed={tier === t} onClick={() => setTier(t)}>{t.toUpperCase()} / {t === 'l0' ? 'Abstract' : t === 'l1' ? 'Overview' : 'Detail'}</button>)}</div>
      <pre>{(tier === 'l0' ? node.abstract : tier === 'l1' ? node.overview : node.content) || (node.kind === 'directory' ? 'This directory groups project context.' : '[empty / forgotten]')}</pre>
      {node.kind !== 'directory' && <div className="inspector-body">
        <details open><summary>Source evidence · {support.length || evidence.length}</summary>
          {(node.status !== 'retracted' && support.length ? support : evidence).map(e => <article key={e.id} className="evidence-quote"><small>v{e.revision} · characters {e.start_char}–{e.end_char} · {e.redacted ? 'redacted' : e.id}</small>
            <blockquote>{e.quote || '[forgotten]'}</blockquote><button className="text-button" onClick={() => open(e.node_id)}>Open original source ↗</button></article>)}</details>
        <details><summary>Version comparison · {versions.length} versions</summary>
          <label>COMPARE WITH<select value={compare} onChange={e => setCompare(e.target.value)}><option value="">Select a revision</option>{versions.map(v => <option key={v.revision} value={v.revision}>Revision {v.revision} · {v.snapshot.status}</option>)}</select></label>
          {historical && <div className="version-columns"><div><small>REVISION {historical.revision}</small><pre>{historical.snapshot.content || '[empty / forgotten]'}</pre></div><div><small>CURRENT v{node.revision}</small><pre>{node.content || '[empty / forgotten]'}</pre></div></div>}</details>
        {node.status === 'active' && ['semantic', 'episodic', 'procedural'].includes(node.kind) && <details><summary>Correct this memory</summary>
          <p className="fine-print">Your statement becomes a new source. The current fact changes only after candidate review.</p>
          <form onSubmit={submitCorrection}><label htmlFor="correction">NEW STATEMENT</label><textarea id="correction" value={correction} onChange={e => setCorrection(e.target.value)} maxLength={4000} required />
            <div className="actions"><button disabled={busy || !correction.trim()}>Propose correction</button><button type="button" className="secondary danger" disabled={busy || !evidence.length} onClick={() => void retract()}>Retract fact</button></div></form></details>}
        {node.status !== 'retracted' && <details><summary className="danger">Forget this source</summary><p className="fine-print">Forgetting redacts stored source history and known dependencies. It cannot undo an external model request already sent.</p>
          <button className="secondary" disabled={busy} onClick={() => { void api<Preview>(`/api/memory/${node.id}/forget-preview`).then(setPreview).catch(e => setError(e.message)); }}>Preview affected memories</button>
          {preview && <div className="forget-preview"><p>{preview.derived_node_ids.length} derived memories will also be redacted.{preview.redacts_messages && ' The original user message will be redacted.'}</p>
            {preview.derived_node_ids.map(id => <button key={id} className="text-button" onClick={() => open(id)}>{id}</button>)}
            <label className="detail-toggle"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />I understand the source history will be redacted</label>
            <button className="danger secondary" disabled={busy || !confirmed} onClick={() => void forget()}>Forget permanently</button></div>}</details>}
      </div>}
    </>}
  </section>;
}
