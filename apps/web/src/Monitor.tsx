import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Activity, RefreshCw } from "lucide-react";
import { api, type Project, type Run, type Job } from "./api";
import { label } from "./ui";
import { RunTrace } from "./RunTrace";

type Record = Run & { created_at: string; thread_id: string; query?: string };
export function Monitor({
  projects,
  initial,
}: {
  projects: Project[];
  initial: string;
}) {
  const [projectId, setProjectId] = useState(initial || projects[0]?.id || "");
  const [runs, setRuns] = useState<Record[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [runId, setRunId] = useState("");
  const [error, setError] = useState("");
  const [tab, setTab] = useState("runs");
  const [filter, setFilter] = useState("");
  const scope = useRef(projectId);
  scope.current = projectId;
  useEffect(() => {
    if (!projectId && projects.length) setProjectId(initial || projects[0].id);
  }, [projects, initial, projectId]);
  async function load() {
    if (!projectId) return;
    try {
      const data = await api<{ runs: Record[]; jobs: Job[] }>(
        `/api/projects/${projectId}/monitor`,
      );
      if (scope.current === projectId) {
        setRuns(data.runs);
        setJobs(data.jobs);
      }
    } catch (e) {
      if (scope.current === projectId) setError((e as Error).message);
    }
  }
  useEffect(() => {
    setRunId("");
    setRuns([]);
    setJobs([]);
    setError("");
    void load();
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [projectId]);
  return (
    <div className="monitor-page">
      <header className="monitor-header">
        <a href="/" className="text-button">
          <ArrowLeft size={16} />
          返回工作台
        </a>
        <span>
          <Activity size={17} />
          项目监控
        </span>
        <select
          aria-label="监控项目"
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
        >
          {projects.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </select>
      </header>
      <div className="monitor-summary">
        <div>
          <small>最近运行</small>
          <strong>{runs.length}</strong>
        </div>
        <div>
          <small>成功运行</small>
          <strong>{runs.filter((r) => r.status === "succeeded").length}</strong>
        </div>
        <div>
          <small>模型报告 token</small>
          <strong>
            {runs
              .reduce((n, r) => n + (r.usage_tokens || 0), 0)
              .toLocaleString()}
          </strong>
        </div>
        <div>
          <small>失败任务</small>
          <strong>{jobs.filter((j) => j.status === "failed").length}</strong>
        </div>
      </div>
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
      <div className="monitor-grid">
        <section className="monitor-list">
          <div className="section-line">
            <div className="segmented">
              <button
                className={tab === "runs" ? "active" : ""}
                onClick={() => setTab("runs")}
              >
                运行
              </button>
              <button
                className={tab === "jobs" ? "active" : ""}
                onClick={() => setTab("jobs")}
              >
                后台任务
              </button>
            </div>
            <button
              aria-label="刷新监控"
              className="icon-button"
              onClick={() => void load()}
            >
              <RefreshCw size={15} />
            </button>
          </div>
          <input
            className="filter-input"
            aria-label="筛选监控记录"
            placeholder="搜索任务、模型或状态…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          {tab === "runs"
            ? runs
                .filter((r) =>
                  `${r.query} ${r.model} ${label(r.status)}`
                    .toLowerCase()
                    .includes(filter.toLowerCase()),
                )
                .map((r) => (
                  <button
                    key={r.id}
                    className={`monitor-row ${r.id === runId ? "active" : ""}`}
                    onClick={() => setRunId(r.id)}
                  >
                    <span>
                      <b>{r.query || r.model}</b>
                      <small>
                        {r.model} ·{" "}
                        {new Date(r.created_at).toLocaleString("zh-CN", {
                          hour12: false,
                        })}
                      </small>
                    </span>
                    <span className={`status ${r.status}`}>
                      {label(r.status)}
                    </span>
                  </button>
                ))
            : jobs
                .filter((j) =>
                  `${label(j.kind)} ${label(j.status)}`.includes(filter),
                )
                .map((j) => (
                  <details className="job-detail" key={j.id}>
                    <summary>
                      {label(j.kind)}
                      <span className={`status ${j.status}`}>
                        {label(j.status)}
                      </span>
                    </summary>
                    <p>尝试 {j.attempts} 次</p>
                    {j.error && <p className="notice">{j.error}</p>}
                    <pre>{JSON.stringify(j.result, null, 2)}</pre>
                  </details>
                ))}
          {!runs.length && tab === "runs" && (
            <p className="quiet-empty">还没有运行记录。</p>
          )}
        </section>
        <div className="monitor-detail">
          {runId ? (
            <RunTrace key={runId} runId={runId} onFinish={() => {}} />
          ) : (
            <div className="quiet-empty">
              <Activity size={30} strokeWidth={1.2} />
              <p>选择一次运行，查看调用链与耗时。</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
