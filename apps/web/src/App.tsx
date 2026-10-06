import { useEffect, useState, type FormEvent } from 'react';

type Project = { id: string; name: string; description: string };
type Node = { id: string; uri: string; parent_uri: string | null; kind: string; title: string;
  abstract: string; overview: string; content?: string; revision: number; source_event_ids: string[] };
type Job = { id: string; status: string; attempts: number; error: string | null };

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json' } });
  const value = await response.json();
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : 'Please check your document path and content.');
  return value;
}

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState('');
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
      if (active) { setProjects(rows); setProjectId(rows[0]?.id || ''); setReady(true); }
    }).catch(e => { if (active) setError(e.message); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    setNodes([]); setSelected(null); setJob(null);
    if (!projectId) return;
    const abort = new AbortController();
    api<Node[]>(`/api/projects/${projectId}/context/tree`, { signal: abort.signal }).then(setNodes)
      .catch(e => { if (!abort.signal.aborted) setError(e.message); });
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
      }).catch(e => { if (active) setError(e.message); });
    }, 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, [job?.id, job?.status, projectId]);

  async function createProject(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      const row = await api<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name }) });
      setProjects(previous => [row, ...previous]); setProjectId(row.id); setName('');
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function ingest(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('');
    try {
      setJob(await api<Job>(`/api/projects/${projectId}/ingest`, { method: 'POST', body: JSON.stringify({
        idempotency_key: crypto.randomUUID(), documents: [{ path, content }],
      }) }));
    } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  }

  async function inspect(node: Node) {
    setSelected(node);
    try {
      const detail = await api<Node>(`/api/memory/${node.id}`);
      setSelected(current => current?.id === detail.id ? detail : current);
    } catch (e) { setError((e as Error).message); }
  }

  const project = projects.find(p => p.id === projectId);
  const resources = nodes.filter(n => n.kind === 'resource');
  return <div className="studio">
    <aside className="rail">
      <a className="wordmark" href="/">si<span>↗</span><small>CONTEXT STUDIO</small></a>
      <div className="rail-label">YOUR WORKSPACES <span>{projects.length.toString().padStart(2, '0')}</span></div>
      <nav aria-label="Projects">{projects.map(p => <button key={p.id} className={p.id === projectId ? 'project active' : 'project'}
        onClick={() => setProjectId(p.id)}><span className="project-icon">P</span>{p.name}</button>)}</nav>
      <form className="new-project" onSubmit={createProject}>
        <label htmlFor="project-name">Create a project</label>
        <input id="project-name" value={name} maxLength={160} placeholder="A place for your ideas" onChange={e => setName(e.target.value)} required />
        <button disabled={busy || !ready}>Add workspace +</button>
      </form>
      <div className="rail-footer"><i className={ready ? 'online' : ''} />{ready ? 'API connected' : 'Connecting to API'}<small>Single user · multiple projects</small></div>
    </aside>
    <main>
      <header><span>WORKSPACE / {project?.name || 'GET STARTED'}</span><span className="phase">FOUNDATION · 02</span></header>
      <section className="intro"><div><p className="eyebrow">KNOWLEDGE WITH A HOME</p><h1>{project?.name || 'Build your project context.'}</h1>
        <p>Give your project a place to remember. Import source material,<br className="desktop"/> follow its origin, and inspect every document.</p></div>
        <div className="resource-count"><strong>{resources.length.toString().padStart(2, '0')}</strong><span>source documents</span></div></section>
      {error && <div className="error" role="alert">{error}<button onClick={() => setError('')} aria-label="Dismiss error">×</button></div>}
      {!projectId ? <div className="empty"><span>01 / START HERE</span><h2>Your context starts with a project.</h2><p>Create a workspace in the sidebar. Each project keeps its documents in a separate namespace.</p></div> : <>
        <div className="workspace-grid">
          <section className="panel import-panel"><div className="panel-heading"><h2>Import a source</h2><span>01</span></div>
            <form onSubmit={ingest}>
              <label htmlFor="path">DOCUMENT PATH</label><input id="path" value={path} onChange={e => setPath(e.target.value)} required maxLength={240}/>
              <label htmlFor="content">SOURCE CONTENT</label><textarea id="content" value={content} onChange={e => setContent(e.target.value)} required
                placeholder={'# Project decisions\n\nPaste Markdown, notes, or source code…'} maxLength={200000}/>
              <div className="form-footer"><span>Markdown · text · code</span><button disabled={busy || ['queued', 'running'].includes(job?.status || '')}>Import document ↗</button></div>
            </form>
            {job && <div className="job" role="status"><b>{job.status}</b><span>Attempt {job.attempts} · {job.id.slice(0, 8)}</span>{job.error && <p>{job.error}</p>}</div>}
          </section>
          <section className="panel tree-panel"><div className="panel-heading"><h2>Context explorer</h2><span>02</span></div>
            <div className="namespace">waku://users/default/projects/…</div>
            {!nodes.length ? <p className="placeholder">Import your first source to create the context tree.</p> : <div className="tree" role="list">
              {nodes.map(n => <button key={n.id} className={selected?.id === n.id ? 'tree-node chosen' : 'tree-node'}
                style={{ paddingLeft: `${12 + Math.max(0, n.uri.split('/').length - 7) * 14}px` }}
                onClick={() => void inspect(n)}><span>{n.kind === 'directory' ? '▱' : '≡'}</span>{n.title}<small>{n.kind === 'resource' ? `v${n.revision}` : ''}</small></button>)}
            </div>}
          </section>
        </div>
        <section className="panel evidence-panel"><div className="panel-heading"><h2>Source inspector</h2><span>03</span></div>
          {selected ? <><div className="source-meta"><h3>{selected.title}</h3><span>{selected.kind} / revision {selected.revision}</span></div>
            <code className="uri">{selected.uri}</code><pre>{selected.content || 'This directory groups project context.'}</pre>
            {!!selected.source_event_ids.length && <p className="provenance">IMPORT EVIDENCE · {selected.source_event_ids.join(', ')}</p>}</> : <p className="placeholder">Select a node to inspect its content and import evidence.</p>}
        </section>
      </>}
      <footer>SI-AGENT <span>Sources are stored as inspectable project context. Semantic retrieval and chat will follow.</span></footer>
    </main>
  </div>;
}
