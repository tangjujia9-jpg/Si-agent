import { label, uiMessage } from './ui';
import { useEffect, useRef, useState } from 'react';
import { api, post, type Candidate, type ProjectNode, type Job, type Evidence } from './api';

function BeforeContent({ nodeId, revision }: { nodeId: string; revision: number | null }) {
  const [content, setContent] = useState('正在加载修改前内容……');
  useEffect(() => {
    let active = true;
    api<{ revision: number; snapshot: { content: string } }[]>(`/api/memory/${nodeId}/versions`)
      .then(rows => { if (active) setContent(rows.find(v => v.revision === revision)?.snapshot.content ?? '无法读取指定版本。'); })
      .catch(e => { if (active) setContent(e.message); });
    return () => { active = false; };
  }, [nodeId, revision]);
  return <pre>{content || '[内容为空或已忘记]'}</pre>;
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
      } catch (e) { if (active) setError(uiMessage((e as Error).message)); }
      if (active) timer = window.setTimeout(() => void load(), 2500);
    }
    void load(); return () => { active = false; window.clearTimeout(timer); };
  }, [projectId, busy]);
  async function decide(candidate: Candidate, action: string) {
    setBusy(candidate.id); setError('');
    try { await post(`/api/memory/candidates/${candidate.id}/${action}`); changed(); }
    catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(''); }
  }
  async function showQuotes(c: Candidate) {
    try { const rows = await api<Evidence[]>(`/api/projects/${projectId}/evidence?${new URLSearchParams({ ids: c.evidence_ids.join(',') })}`); setQuotes(q => ({ ...q, [c.id]: rows })); }
    catch (e) { setError(uiMessage((e as Error).message)); }
  }
  return <div className="review-layout"><section className="panel"><div className="panel-heading"><h2>记忆审核</h2><span>{candidates.filter(c => c.status === 'pending' && !c.job_id).length} 条待审核</span></div>
    {error && <p role="alert" className="notice">{error}</p>}
    {!candidates.length && <p className="placeholder">暂无候选记忆。可以导入项目决策，或从会话中抽取记忆。</p>}
    {candidates.map(c => {
      const current = nodes.find(n => n.uri === c.uri);
      return <article className="candidate" key={c.id}><div className="hit-heading"><strong>{c.title}</strong><span className={`status ${c.status}`}>{label(c.job_id && c.status === 'pending' ? 'queued' : c.status)} · {label(c.action)}</span></div>
        <code>{c.uri}</code>{c.reason && <p className="fine-print">{uiMessage(c.reason)}</p>}
        <div className="version-columns">{current && <div><small>修改前 · v{c.expected_revision ?? current.revision}</small><BeforeContent nodeId={current.id} revision={c.expected_revision ?? current.revision} /><button className="text-button" onClick={() => open(current.id)}>检查当前记忆 ↗</button></div>}
          <div><small>{c.action === 'RETRACT' ? '撤回内容' : '候选内容'}</small><p className="candidate-content">{c.content || '[空]'}</p><small>预期正文版本： {c.expected_revision ?? '新记忆'}</small></div></div>
        <button className="text-button" onClick={() => void showQuotes(c)}>查看证据（{c.evidence_ids.length}）</button>
        {quotes[c.id]?.map(e => <blockquote key={e.id}>{e.quote || '[已脱敏]'}<button className="text-button" onClick={() => open(e.node_id)}>打开来源 ↗</button></blockquote>)}
        {c.status === 'pending' && !c.job_id && <div className="actions"><button disabled={!!busy} onClick={() => void decide(c, 'apply')}>接受候选</button><button className="secondary" disabled={!!busy} onClick={() => void decide(c, 'skip')}>跳过候选</button></div>}
        {c.node_id && <button className="text-button" onClick={() => open(c.node_id!)}>打开已生成记忆 ↗</button>}
      </article>;
    })}</section><div><section className="panel"><div className="panel-heading"><h2>待复核记忆</h2><span>来源已变化</span></div>
      {nodes.filter(n => n.needs_review).map(n => <button key={n.id} className="review-node" onClick={() => open(n.id)}>{n.title}<small>暂不参与检索 · v{n.revision}</small></button>)}
      {!nodes.some(n => n.needs_review) && <p className="placeholder">没有需要复核来源的记忆。</p>}</section>
      <section className="panel jobs-panel"><div className="panel-heading"><h2>后台记忆任务</h2><span>最近 30 个任务</span></div>
        {jobs.map(j => <details className="job-detail" key={j.id}><summary>{label(j.kind)}<span className="status">{label(j.status)}</span></summary><p className="fine-print">{j.id} · 尝试次数 {j.attempts}</p>{j.error && <p className="notice">{uiMessage(j.error)}</p>}<pre>{JSON.stringify(j.result, null, 2)}</pre></details>)}
        {!jobs.length && <p className="placeholder">这里会显示资料抽取、记忆合并与索引任务。</p>}</section></div></div>;
}
