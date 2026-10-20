import { useEffect, useRef, useState } from "react";
import { Check, FileText, FolderOpen, RefreshCw, Upload } from "lucide-react";
import { api, post, type Job, type Project, type ProjectNode } from "./api";
import { batches, fileText, readSelection, type SourceFile } from "./files";
import { nodeTitle, label } from "./ui";
import { ContextTree } from "./ContextTree";

export function Sources({
  project,
  nodes,
  initial,
  inspect,
  changed,
}: {
  project: Project;
  nodes: ProjectNode[];
  initial: SourceFile[];
  inspect: (node: ProjectNode) => void;
  changed: () => void;
}) {
  const [files, setFiles] = useState<SourceFile[]>(initial);
  const [selected, setSelected] = useState<Set<string>>(
    new Set(initial.map((f) => f.path)),
  );
  const [skipped, setSkipped] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [search, setSearch] = useState("");
  const plan = useRef<
    { documents: { path: string; content: string }[]; key: string; job?: Job }[]
  >([]);
  const fileInput = useRef<HTMLInputElement>(null),
    folderInput = useRef<HTMLInputElement>(null);
  const running = jobs.some((j) => ["queued", "running"].includes(j.status));
  useEffect(() => {
    folderInput.current?.setAttribute("webkitdirectory", "");
  }, []);
  useEffect(() => {
    if (!running) return;
    const abort = new AbortController();
    const timer = window.setInterval(() => {
      void Promise.all(
        jobs.map((j) =>
          api<Job>(`/api/jobs/${j.id}`, { signal: abort.signal }),
        ),
      )
        .then((rows) => {
          setJobs(rows);
          if (rows.every((j) => !["queued", "running"].includes(j.status)))
            changed();
        })
        .catch((e) => {
          if (!abort.signal.aborted) setError(e.message);
        });
    }, 1200);
    return () => {
      abort.abort();
      clearInterval(timer);
    };
  }, [running, jobs.map((j) => j.id).join(",")]);
  async function pick(list: FileList | null) {
    if (!list) return;
    const result = await readSelection(list);
    plan.current = [];
    setFiles(result.accepted);
    setSelected(new Set(result.accepted.map((f) => f.path)));
    setSkipped(result.skipped.length);
    setJobs([]);
  }
  async function scan() {
    setBusy(true);
    setError("");
    try {
      const result = await api<{
        files: SourceFile[];
        skipped: number;
        truncated: boolean;
      }>(`/api/projects/${project.id}/workspace`);
      plan.current = [];
      setFiles(result.files);
      setSelected(new Set(result.files.map((f) => f.path)));
      setSkipped(result.skipped);
      if (result.truncated)
        setError("清单已限制为 500 个文件；请先选择本次需要导入的资料。");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function ingest() {
    setBusy(true);
    setError("");
    const submitted: Job[] = [];
    try {
      if (!plan.current.length) {
        const documents = [];
        for (const file of files.filter((f) => selected.has(f.path))) {
          const content =
            file.file || file.content !== undefined
              ? await fileText(file)
              : (
                  await api<{ content: string }>(
                    `/api/projects/${project.id}/workspace/file?path=${encodeURIComponent(file.path)}`,
                  )
                ).content;
          documents.push({ path: file.path, content });
        }
        plan.current = batches(documents).map((documents) => ({
          documents,
          key: crypto.randomUUID(),
        }));
      }
      for (const batch of plan.current) {
        batch.job =
          batch.job ||
          (await post<Job>(`/api/projects/${project.id}/ingest`, {
            idempotency_key: batch.key,
            documents: batch.documents,
          }));
        submitted.push(batch.job);
        setJobs([...submitted]);
      }
      setFiles([]);
      setSelected(new Set());
      plan.current = [];
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const resources = nodes.filter(
    (n) =>
      n.kind === "resource" &&
      n.title.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className="sources-view">
      <div className="source-actions">
        <button
          className="secondary"
          disabled={busy || running}
          onClick={() => fileInput.current?.click()}
        >
          <Upload size={15} />
          上传文件
        </button>
        <button
          className="secondary"
          disabled={busy || running}
          onClick={() => folderInput.current?.click()}
        >
          <FolderOpen size={15} />
          上传文件夹
        </button>
        {project.root_path && (
          <button
            className="secondary"
            disabled={busy || running}
            onClick={() => void scan()}
          >
            <RefreshCw size={14} />
            扫描项目
          </button>
        )}
      </div>
      <p className="muted">支持 Markdown、文本和代码文件（UTF-8）。</p>
      <input
        hidden
        type="file"
        multiple
        ref={fileInput}
        aria-label="上传文件"
        onChange={(e) => void pick(e.target.files)}
      />
      <input
        hidden
        type="file"
        multiple
        ref={folderInput}
        aria-label="上传文件夹"
        onChange={(e) => void pick(e.target.files)}
      />
      {files.length > 0 && (
        <section className="import-preview">
          <div className="section-line">
            <h3>确认导入</h3>
            <span>
              {selected.size} / {files.length} 个文件
            </span>
          </div>
          <p className="muted">
            已过滤 {skipped} 个隐藏、敏感、非文本或超限文件。
          </p>
          <div className="file-preview-list">
            {files.map((f) => (
              <label key={f.path}>
                <input
                  type="checkbox"
                  checked={selected.has(f.path)}
                  disabled={busy || running}
                  onChange={(e) => {
                    plan.current = [];
                    setSelected((old) => {
                      const next = new Set(old);
                      if (e.target.checked) next.add(f.path);
                      else next.delete(f.path);
                      return next;
                    });
                  }}
                />
                <span>{f.path}</span>
                <small>{(f.size / 1024).toFixed(1)} KB</small>
              </label>
            ))}
          </div>
          <div className="actions">
            <button
              disabled={busy || running || !selected.size}
              onClick={() => void ingest()}
            >
              {busy ? "正在提交…" : "导入所选资料"}
            </button>
            <button className="text-button" onClick={() => setFiles([])}>
              取消
            </button>
          </div>
        </section>
      )}
      {jobs.map((j) => (
        <div className="import-job" role="status" key={j.id}>
          {j.status === "succeeded" ? (
            <Check size={14} />
          ) : (
            <RefreshCw size={14} className={running ? "spin" : ""} />
          )}
          资料导入 · {label(j.status)}
          {j.error && <span>{j.error}</span>}
        </div>
      ))}
      {jobs.some((j) => j.status === "succeeded") && (
        <p className="muted">
          原文已保存，摘要和向量索引继续在后台建立。进度可在项目监控查看。
        </p>
      )}
      {error && (
        <p role="alert" className="notice">
          {error}
        </p>
      )}
      <div className="section-line">
        <h3>已导入资料 · {resources.length}</h3>
        <button className="icon-button" aria-label="刷新资料" onClick={changed}>
          <RefreshCw size={14} />
        </button>
      </div>
      <input
        className="filter-input"
        aria-label="筛选资料"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        placeholder="搜索文件名…"
      />
      {!resources.length ? (
        <div className="quiet-empty">
          <FileText size={27} strokeWidth={1.3} />
          <p>项目资料会显示在这里。</p>
        </div>
      ) : (
        <div className="source-list">
          {resources.map((n) => (
            <button key={n.id} onClick={() => inspect(n)}>
              <FileText size={16} />
              <span>
                <b>{nodeTitle(n)}</b>
                <small>
                  {decodeURIComponent(n.uri.split("/resources/")[1] || "")}
                </small>
              </span>
              <small>v{n.revision}</small>
            </button>
          ))}
        </div>
      )}
      {nodes.length > 0 && (
        <details className="context-directory">
          <summary>浏览上下文目录</summary>
          <ContextTree nodes={nodes} inspect={inspect} />
        </details>
      )}
    </div>
  );
}
