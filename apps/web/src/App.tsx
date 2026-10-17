import { label, nodeTitle, uiMessage } from './ui';
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { MemorySearch } from './MemorySearch';
import { api, type ProjectNode as Node, type Job } from './api';
import { Chat } from './Chat';
import { MemoryInspector } from './MemoryInspector';
import { MemoryReview } from './MemoryReview';

type Project = { id: string; name: string; description: string };
export function App() {
  const [tab, setTab] = useState('chat');
  const [revision, setRevision] = useState(0);
  const [projects, set项目列表] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
  const currentProject = useRef(projectId); currentProject.current = projectId;
  const [nodes, setNodes] = useState<Node[]>([]);
  const [selected, setSelected] = useState<Node | null>(null);
  const [name, setName] = useState('');
  const [path, setPath] = useState('docs/architecture.md');
  const [content, setContent] = useState('');
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let active = true;
    Promise.all([api<Project[]>('/api/projects'), api('/ready')]).then(([rows]) => {
      if (active) { set项目列表(rows); setProjectId(rows[0]?.id || ''); setReady(true); }
    }).catch(e => { if (active) setError(uiMessage(e.message)); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    setNodes([]); setSelected(null); setJob(null);
    if (!projectId) return;
    const abort = new AbortController();
    api<Node[]>(`/api/projects/${projectId}/context/tree`, { signal: abort.signal }).then(setNodes)
      .catch(e => { if (!abort.signal.aborted) setError(uiMessage(e.message)); });
    return () => abort.abort();
  }, [projectId]);

  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return;
    let active = true;
    const timer = window.setInterval(() => {
      api<Job>(`/api/jobs/${job.id}`).then(async updated => {
        if (!active) return;
        if (updated.status === 'succeeded') {
          const rows = await api<Node[]>(`/api/projects/${projectId}/context/tree`);
          if (active) setNodes(rows);
        }
        if (active) setJob(updated);
      }).catch(e => { if (active) setError(uiMessage(e.message)); });
    }, 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, [job?.id, job?.status, projectId]);

  async function createProject(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const row = await api<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name }) });
      set项目列表(previous => [row, ...previous]); setProjectId(row.id); setName('');
    } catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(false); }
  }

  async function ingest(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      setJob(await api<Job>(`/api/projects/${projectId}/ingest`, { method: 'POST', body: JSON.stringify({
        idempotency_key: crypto.randomUUID(), documents: [{ path, content }],
      }) }));
    } catch (e) { setError(uiMessage((e as Error).message)); } finally { setBusy(false); }
  }

  async function inspect(node: Node) {
    const scope = projectId;
    setSelected(node);
    try {
      const detail = await api<Node>(`/api/memory/${node.id}`);
      if (currentProject.current === scope) setSelected(current => current?.id === detail.id ? detail : current);
    } catch (e) { setError(uiMessage((e as Error).message)); }
  }

  const refresh = useCallback(() => {
    if (!projectId) return;
    void api<Node[]>(`/api/projects/${projectId}/context/tree`).then(rows => { if (currentProject.current === projectId) { setNodes(rows); setRevision(r => r + 1); } }).catch(e => { if (currentProject.current === projectId) setError(uiMessage(e.message)); });
  }, [projectId]);

  async function inspectId(id: string) {
    const scope = projectId;
    try { const node = await api<Node>(`/api/memory/${id}`); if (currentProject.current === scope) await inspect(node); } catch (e) { if (currentProject.current === scope) setError(uiMessage((e as Error).message)); }
  }
  function openUri(uri: string) {
    const node = nodes.find(n => n.uri === uri);
    if (node) void inspect(node); else setError('当前项目中没有可打开的有效引用来源，请刷新后重试。');
  }

  const project = projects.find(p => p.id === projectId);
  const resources = nodes.filter(n => n.kind === 'resource');
  return <div className="studio">
    <aside className="rail">
      <a className="wordmark" href="/">si<span>↗</span><small>项目上下文工作台</small></a>
      <div className="rail-label">我的项目 <span>{projects.length.toString().padStart(2, '0')}</span></div>
      <nav aria-label="项目列表">{projects.map(p => <button key={p.id} className={p.id === projectId ? 'project active' : 'project'}
        onClick={() => setProjectId(p.id)}><span className="project-icon">P</span>{p.name}</button>)}</nav>
      <form className="new-project" onSubmit={createProject}>
        <label htmlFor="project-name">创建项目</label>
        <input id="project-name" value={name} maxLength={160} placeholder="输入项目名称" onChange={e => setName(e.target.value)} required />
        <button disabled={busy || !ready}>添加项目 +</button>
      </form>
      <div className="rail-footer"><i className={ready ? 'online' : ''} />{ready ? '后端已连接' : '正在连接后端'}<small>单用户 · 多项目</small></div>
    </aside>
    <main>
      <header><span>项目 / {project?.name || '开始使用'}</span><span className="phase">项目开发智能体</span></header>
      <section className="intro"><div><p className="eyebrow">让项目知识有据可查</p><h1>{project?.name || '建立你的项目上下文'}</h1>
        <p>依据项目资料展开对话。<br className="desktop"/> 让每项决策可检查、可纠错、可记忆。</p></div>
        <div className="resource-count"><strong>{resources.length.toString().padStart(2, '0')}</strong><span>份来源文档</span></div></section>
      {error && <div className="error" role="alert">{error}<button onClick={() => setError('')} aria-label="关闭错误提示">×</button></div>}
      {!projectId ? <div className="empty"><span>01 / 从这里开始</span><h2>从创建项目开始积累知识。</h2><p>在左侧创建项目。每个项目拥有独立的资料与记忆空间。</p></div> : <>
        <div className="workspace-nav" role="tablist" aria-label="项目功能">{[['chat', '项目对话'], ['context', '上下文浏览'], ['review', '记忆审核']].map(([id, label]) => <button key={id} role="tab" aria-selected={tab === id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}{id === 'review' && nodes.some(n => n.needs_review) ? ' · 待复核' : ''}</button>)}<button className="refresh-button" onClick={refresh}>刷新 ↻</button></div>
        <div role="tabpanel">
        {tab === 'chat' && <Chat key={projectId} projectId={projectId} open={openUri} changed={refresh} />}
        {tab === 'review' && <MemoryReview key={projectId} projectId={projectId} nodes={nodes} open={id => void inspectId(id)} changed={refresh} />}
        {tab === 'context' && <><div className="workspace-grid">
          <section className="panel import-panel"><div className="panel-heading"><h2>导入资料</h2><span>01</span></div>
            <form onSubmit={ingest}>
              <label htmlFor="path">文档路径</label><input id="path" value={path} onChange={e => setPath(e.target.value)} required maxLength={240}/>
              <label htmlFor="content">资料正文</label><textarea id="content" value={content} onChange={e => setContent(e.target.value)} required
                placeholder={'# 项目决策\n\n粘贴 Markdown、笔记或源代码……'} maxLength={200000}/>
              <div className="form-footer"><span>Markdown · 文本 · 代码</span><button disabled={busy || ['queued', 'running'].includes(job?.status || '')}>导入文档 ↗</button></div>
            </form>
            {job && <div className="job" role="status"><b>{label(job.status)}</b><span>尝试次数 {job.attempts} · {job.id.slice(0, 8)}</span>{job.error && <p>{uiMessage(job.error)}</p>}</div>}
          </section>
          <section className="panel tree-panel"><div className="panel-heading"><h2>上下文浏览</h2><span>02</span></div>
            <div className="namespace">si://users/default/projects/…</div>
            {!nodes.length ? <p className="placeholder">导入第一份资料后，这里会显示上下文目录。</p> : <div className="tree" role="list">
              {nodes.map(n => <button key={n.id} className={selected?.id === n.id ? 'tree-node chosen' : 'tree-node'}
                style={{ paddingLeft: `${12 + Math.max(0, n.uri.split('/').length - 7) * 14}px` }}
                onClick={() => void inspect(n)}><span>{n.kind === 'directory' ? '▱' : '≡'}</span>{nodeTitle(n)}<small>{n.kind === 'resource' ? `v${n.revision}` : ''}</small></button>)}
            </div>}
          </section>
        </div>
        <MemorySearch projectId={projectId} inspect={id => void inspectId(id)} />
        </>}
        </div>
        {selected && <MemoryInspector key={projectId + selected.id} nodeId={selected.id} projectId={projectId} revision={revision} changed={refresh} open={id => void inspectId(id)} />}
      </>}
      <footer>SI-AGENT <span>项目隔离记忆 · 回答附带引用 · 操作全程可查</span></footer>
    </main>
  </div>;
}
