import { useCallback, useEffect, useRef, useState } from "react";
import {
  Activity,
  ArrowLeft,
  BookOpen,
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  Menu,
  MessageSquare,
  Plus,
  Search,
  SquarePen,
  X,
} from "lucide-react";
import { api, post, type Project, type ProjectNode, type Thread } from "./api";
import { Chat } from "./Chat";
import { ProjectDialog } from "./ProjectDialog";
import { Sources } from "./Sources";
import { MemoryReview } from "./MemoryReview";
import { MemoryInspector } from "./MemoryInspector";
import { MemorySearch } from "./MemorySearch";
import { Monitor } from "./Monitor";
import type { SourceFile } from "./files";

export function App() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectId, setProjectId] = useState("");
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadId, setThreadId] = useState("");
  const [nodes, setNodes] = useState<ProjectNode[]>([]);
  const [drawer, setDrawer] = useState("");
  const [selected, setSelected] = useState<ProjectNode | null>(null);
  const [dialog, setDialog] = useState(false);
  const [error, setError] = useState("");
  const [sidebar, setSidebar] = useState(false);
  const [revision, setRevision] = useState(0);
  const [initialFiles, setInitialFiles] = useState<SourceFile[]>([]);
  const [system, setSystem] = useState<{
    database: string;
    vector_version?: string;
    workspace_enabled?: boolean;
  } | null>(null);
  const scope = useRef(projectId);
  scope.current = projectId;
  const pendingSources = useRef("");
  const project = projects.find((p) => p.id === projectId);
  useEffect(() => {
    let active = true;
    void Promise.all([
      api<Project[]>("/api/projects"),
      api<typeof system>("/api/system"),
    ])
      .then(([rows, status]) => {
        if (!active) return;
        setProjects(rows);
        setSystem(status);
        const query = new URLSearchParams(location.search).get("project");
        setProjectId(
          rows.find((p) => p.id === query)?.id ||
            rows.find((p) => p.id === localStorage.getItem("si-project"))?.id ||
            rows[0]?.id ||
            "",
        );
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, []);
  useEffect(() => {
    setThreads([]);
    setThreadId("");
    setNodes([]);
    setSelected(null);
    setDrawer(pendingSources.current === projectId ? "sources" : "");
    pendingSources.current = "";
    if (!projectId) return;
    localStorage.setItem("si-project", projectId);
    const abort = new AbortController();
    void Promise.all([
      api<Thread[]>(`/api/projects/${projectId}/threads`, {
        signal: abort.signal,
      }),
      api<ProjectNode[]>(`/api/projects/${projectId}/context/tree`, {
        signal: abort.signal,
      }),
    ])
      .then(([rows, tree]) => {
        if (abort.signal.aborted) return;
        setThreads(rows);
        setNodes(tree);
        setThreadId(
          rows.find(
            (t) => t.id === localStorage.getItem(`si-thread:${projectId}`),
          )?.id ||
            rows[0]?.id ||
            "",
        );
      })
      .catch((e) => {
        if (!abort.signal.aborted) setError(e.message);
      });
    return () => abort.abort();
  }, [projectId]);
  const refresh = useCallback(() => {
    if (!projectId) return;
    void Promise.all([
      api<ProjectNode[]>(`/api/projects/${projectId}/context/tree`),
      api<Thread[]>(`/api/projects/${projectId}/threads`),
    ])
      .then(([tree, rows]) => {
        if (scope.current === projectId) {
          setNodes(tree);
          setThreads(rows);
          setRevision((n) => n + 1);
        }
      })
      .catch((e) => setError(e.message));
  }, [projectId]);
  async function inspectId(id: string) {
    const current = projectId;
    try {
      const node = await api<ProjectNode>(`/api/memory/${id}`);
      if (scope.current === current) {
        setSelected(node);
        setDrawer((value) => value || "sources");
      }
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function openUri(uri: string) {
    const node = nodes.find((n) => n.uri === uri);
    if (node) void inspectId(node.id);
    else {
      refresh();
      setError("当前来源已变化，请刷新资料后重试。");
    }
  }
  async function newThread() {
    if (!projectId) {
      setDialog(true);
      return;
    }
    try {
      const thread = await post<Thread>("/api/threads", {
        project_id: projectId,
        title: "新会话",
      });
      setThreads((rows) => [thread, ...rows]);
      setThreadId(thread.id);
      setSidebar(false);
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function showDrawer(name: string) {
    setSelected(null);
    setDrawer((current) => (current === name ? "" : name));
  }
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setDrawer("");
        setSelected(null);
        setSidebar(false);
      }
    };
    document.addEventListener("keydown", escape);
    return () => document.removeEventListener("keydown", escape);
  }, []);
  if (location.pathname === "/monitor")
    return <Monitor projects={projects} initial={projectId} />;
  return (
    <div className={`workspace ${sidebar ? "sidebar-open" : ""}`}>
      {sidebar && (
        <button
          className="sidebar-scrim"
          aria-label="关闭导航"
          onClick={() => setSidebar(false)}
        />
      )}
      <aside className="sidebar">
        <div className="brand">
          <a href="/" aria-label="Si-agent 首页">
            <span className="brand-symbol">sı</span>
            <b>Si-agent</b>
          </a>
          <button
            className="icon-button"
            aria-label="添加项目"
            onClick={() => setDialog(true)}
          >
            <Plus size={18} />
          </button>
        </div>
        <button className="new-thread" onClick={() => void newThread()}>
          <SquarePen size={17} />
          新对话
        </button>
        <div className="sidebar-section">
          <span>项目</span>
          <button
            className="icon-button"
            aria-label="新建项目"
            onClick={() => setDialog(true)}
          >
            <Plus size={14} />
          </button>
        </div>
        <nav className="project-navigation" aria-label="项目与会话">
          {projects.map((p) => (
            <div key={p.id} className="project-group">
              <button
                className={`project-link ${p.id === projectId ? "selected" : ""}`}
                onClick={() => {
                  setInitialFiles([]);
                  setProjectId(p.id);
                  setSidebar(false);
                }}
              >
                <Folder size={16} />
                <span>{p.name}</span>
                {p.id === projectId ? (
                  <ChevronDown size={13} />
                ) : (
                  <ChevronRight size={13} />
                )}
              </button>
              {p.id === projectId && (
                <div className="thread-list">
                  {threads.map((t) => (
                    <button
                      key={t.id}
                      className={t.id === threadId ? "selected" : ""}
                      onClick={() => {
                        setThreadId(t.id);
                        setSidebar(false);
                      }}
                    >
                      <MessageSquare size={13} />
                      <span>
                        {t.title === "New conversation" ? "新会话" : t.title}
                      </span>
                    </button>
                  ))}
                  {!threads.length && (
                    <button onClick={() => void newThread()}>
                      <Plus size={13} />
                      开启第一段对话
                    </button>
                  )}
                </div>
              )}
            </div>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <a
            href={`/monitor?project=${projectId}`}
            target="_blank"
            rel="noreferrer"
          >
            <Activity size={16} />
            项目监控
            <ChevronRight size={13} />
          </a>
          <div className="backend-status">
            <i className={system?.database === "postgresql" ? "online" : ""} />
            <span>
              {system
                ? system.database === "postgresql"
                  ? `PostgreSQL · pgvector ${system.vector_version || ""}`
                  : "SQLite · 开发模式"
                : "正在连接后端"}
            </span>
          </div>
        </div>
      </aside>
      <main className="main-workspace">
        <header className="workspace-header">
          <button
            className="icon-button mobile-menu"
            aria-label="打开导航"
            onClick={() => setSidebar((v) => !v)}
          >
            <Menu size={18} />
          </button>
          <div className="project-heading">
            <FolderOpen size={17} />
            <strong>{project?.name || "工作台"}</strong>
            <span className="folder-mode">
              {project?.root_path ? "本地项目" : project ? "资料快照" : ""}
            </span>
          </div>
          <div className="header-actions">
            <button
              className={drawer === "sources" ? "active" : ""}
              disabled={!project}
              onClick={() => showDrawer("sources")}
            >
              <FolderOpen size={16} />
              <span>项目资料</span>
            </button>
            <button
              className={drawer === "memory" ? "active" : ""}
              disabled={!project}
              onClick={() => showDrawer("memory")}
            >
              <BookOpen size={16} />
              <span>项目记忆</span>
            </button>
            <button
              className="icon-button"
              disabled={!project}
              aria-label="检索资料"
              onClick={() => showDrawer("search")}
            >
              <Search size={17} />
            </button>
          </div>
        </header>
        {error && (
          <div className="app-error" role="alert">
            {error}
            <button
              className="icon-button"
              aria-label="关闭错误"
              onClick={() => setError("")}
            >
              <X size={14} />
            </button>
          </div>
        )}
        {project ? (
          <Chat
            key={projectId}
            projectId={projectId}
            threadId={threadId}
            onThread={(thread) => {
              setThreads((rows) => [thread, ...rows]);
              setThreadId(thread.id);
            }}
            open={openUri}
            changed={refresh}
            onSources={() => showDrawer("sources")}
          />
        ) : (
          <div className="workspace-empty">
            <span className="brand-symbol">sı</span>
            <h1>给项目一个共同的工作空间。</h1>
            <p>关联文件夹，从第一段对话开始。</p>
            <button onClick={() => setDialog(true)}>
              <FolderOpen size={16} />
              添加项目
            </button>
          </div>
        )}
      </main>
      {drawer && project && (
        <aside
          className="context-drawer"
          aria-label={
            selected
              ? "来源面板"
              : drawer === "sources"
                ? "项目资料"
                : drawer === "memory"
                  ? "项目记忆"
                  : "检索资料"
          }
        >
          <div className="drawer-heading">
            {selected ? (
              <button className="text-button" onClick={() => setSelected(null)}>
                <ArrowLeft size={16} />
                返回
              </button>
            ) : (
              <h2>
                {drawer === "sources"
                  ? "项目资料"
                  : drawer === "memory"
                    ? "项目记忆"
                    : "检索资料"}
              </h2>
            )}
            <button
              className="icon-button"
              aria-label="关闭侧栏"
              onClick={() => {
                setDrawer("");
                setSelected(null);
              }}
            >
              <X size={18} />
            </button>
          </div>
          <div className="drawer-body">
            {selected ? (
              <MemoryInspector
                key={selected.id}
                nodeId={selected.id}
                projectId={projectId}
                revision={revision}
                changed={refresh}
                open={(id) => void inspectId(id)}
              />
            ) : drawer === "sources" ? (
              <Sources
                key={projectId}
                project={project}
                nodes={nodes}
                initial={initialFiles}
                inspect={setSelected}
                changed={refresh}
              />
            ) : drawer === "memory" ? (
              <MemoryReview
                key={projectId}
                projectId={projectId}
                nodes={nodes}
                open={(id) => void inspectId(id)}
                changed={refresh}
              />
            ) : (
              <MemorySearch
                projectId={projectId}
                inspect={(id) => void inspectId(id)}
              />
            )}
          </div>
        </aside>
      )}
      {dialog && (
        <ProjectDialog
          close={() => setDialog(false)}
          created={(p, files) => {
            pendingSources.current = p.id;
            setProjects((rows) => [p, ...rows]);
            setInitialFiles(files);
            setProjectId(p.id);
            setDialog(false);
          }}
        />
      )}
    </div>
  );
}
