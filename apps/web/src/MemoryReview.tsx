import { useEffect, useRef, useState } from 'react';
import { api, post, type Candidate, type ProjectNode, type Job, type Evidence } from './api';

function BeforeContent({ nodeId, revision }: { nodeId: string; revision: number | null }) {
  const [content, setContent] = useState('Loading previous content…');
  useEffect(() => {
    let active = true;
    api<{ revision: number; snapshot: { content: string } }[]>(`/api/memory/${nodeId}/versions`)
      .then(rows => { if (active) setContent(rows.find(v => v.revision === revision)?.snapshot.content ?? 'Requested revision is unavailable.'); })
      .catch(e => { if (active) setContent(e.message); });
    return () => { active = false; };
  }, [nodeId, revision]);
  return <pre>{content || '[empty / forgotten]'}</pre>;
}

export function MemoryReview({ projectId, nodes, changed, open }: { projectId: string; nodes: ProjectNode[]; changed: () => void; open: (id: string) => void }) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState('');
  const [quotes, setQuotes] = useState<Record<string, Evidence[]>>({});
  const signature = useRef('');
  const notify = useRef(changed); notify.current = changed;
  useEffect(() => {
    let active = true; let timer: number;
    async function load() {
      try {
        const [rows, tasks] = await Promise.all([api<Candidate[]>(`/api/projects/${projectId}/memory/candidates`), api<Job[]>(`/api/projects/${projectId}/jobs`)]);
        if (!active) return;
        setCandidates(rows); setJobs(tasks);
        const next = JSON.stringify([rows.map(c => [c.id, c.status, c.job_id]), tasks.map(j => [j.id, j.status])]);
        if (signature.current && signature.current !== next) { setQuotes({}); notify.current(); }
        signature.current = next;
      } catch (e) { if (active) setError((e as Error).message); }
      if (active) timer = window.setTimeout(() => void load(), 2500);
    }
    void load(); return () => { active = false; window.clearTimeout(timer); };
  }, [projectId, busy]);
  async function decide(candidate: Candidate, action: string) {
    setBusy(candidate.id); setError('');
    try { await post(`/api/memory/candidates/${candidate.id}/${action}`); changed(); }
    catch (e) { setError((e as Error).message); } finally { setBusy(''); }
  }
  async function showQuotes(c: Candidate) {
    try { const rows = await api<Evidence[]>(`/api/projects/${projectId}/evidence?${new URLSearchParams({ ids: c.evidence_ids.join(',') })}`); setQuotes(q => ({ ...q, [c.id]: rows })); }
    catch (e) { setError((e as Error).message); }
  }
  return <div className="review-layout"><section className="panel"><div className="panel-heading"><h2>Memory review</h2><span>{candidates.filter(c => c.status === 'pending' && !c.job_id).length} AWAITING REVIEW</span></div>
    {error && <p role="alert" className="notice">{error}</p>}
    {!candidates.length && <p className="placeholder">No candidates yet. Import a decision or extract memory from a conversation.</p>}
    {candidates.map(c => {
      const current = nodes.find(n => n.uri === c.uri);
      return <article className="candidate" key={c.id}><div className="hit-heading"><strong>{c.title}</strong><span className={`status ${c.status}`}>{c.job_id && c.status === 'pending' ? 'queued' : c.status} · {c.action}</span></div>
        <code>{c.uri}</code>{c.reason && <p className="fine-print">{c.reason}</p>}
        <div className="version-columns">{current && <div><small>BEFORE · v{c.expected_revision ?? current.revision}</small><BeforeContent nodeId={current.id} revision={c.expected_revision ?? current.revision} /><button className="text-button" onClick={() => open(current.id)}>Inspect current memory ↗</button></div>}
          <div><small>{c.action === 'RETRACT' ? 'RETRACTION' : 'PROPOSED CONTENT'}</small><p className="candidate-content">{c.content || '[empty]'}</p><small>Expected revision: {c.expected_revision ?? 'new memory'}</small></div></div>
        <button className="text-button" onClick={() => void showQuotes(c)}>Inspect evidence ({c.evidence_ids.length})</button>
        {quotes[c.id]?.map(e => <blockquote key={e.id}>{e.quote || '[redacted]'}<button className="text-button" onClick={() => open(e.node_id)}>Open source ↗</button></blockquote>)}
        {c.status === 'pending' && !c.job_id && <div className="actions"><button disabled={!!busy} onClick={() => void decide(c, 'apply')}>Accept candidate</button><button className="secondary" disabled={!!busy} onClick={() => void decide(c, 'skip')}>Skip candidate</button></div>}
        {c.node_id && <button className="text-button" onClick={() => open(c.node_id!)}>Open resulting memory ↗</button>}
      </article>;
    })}</section><div><section className="panel"><div className="panel-heading"><h2>Needs review</h2><span>SOURCE CHANGED</span></div>
      {nodes.filter(n => n.needs_review).map(n => <button key={n.id} className="review-node" onClick={() => open(n.id)}>{n.title}<small>Excluded from retrieval · v{n.revision}</small></button>)}
      {!nodes.some(n => n.needs_review) && <p className="placeholder">No memories are waiting for source review.</p>}</section>
      <section className="panel jobs-panel"><div className="panel-heading"><h2>Memory activity</h2><span>RECENT 30 JOBS</span></div>
        {jobs.map(j => <details className="job-detail" key={j.id}><summary>{j.kind}<span className="status">{j.status}</span></summary><p className="fine-print">{j.id} · attempt {j.attempts}</p>{j.error && <p className="notice">{j.error}</p>}<pre>{JSON.stringify(j.result, null, 2)}</pre></details>)}
        {!jobs.length && <p className="placeholder">Source extraction, consolidation and indexing activity will appear here.</p>}</section></div></div>;
}
