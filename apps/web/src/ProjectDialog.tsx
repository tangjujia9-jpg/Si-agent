import { useEffect, useRef, useState } from "react";
import { ArrowLeft, Folder, FolderOpen, Upload, X } from "lucide-react";
import { api, post, type Project } from "./api";
import { readSelection, type SourceFile } from "./files";

type Directory = {
  path: string | null;
  parent: string | null;
  directories: { name: string; path: string }[];
};
export function ProjectDialog({
  close,
  created,
}: {
  close: () => void;
  created: (project: Project, files: SourceFile[]) => void;
}) {
  const [name, setName] = useState("");
  const [directory, setDirectory] = useState<Directory | null>(null);
  const [folder, setFolder] = useState("");
  const [files, setFiles] = useState<SourceFile[]>([]);
  const [skipped, setSkipped] = useState(0);
  const [mode, setMode] = useState<"local" | "snapshot">("local");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const picker = useRef<HTMLInputElement>(null);
  const dialog = useRef<HTMLElement>(null);
  useEffect(() => {
    picker.current?.setAttribute("webkitdirectory", "");
    void navigate();
  }, []);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    return () => previous?.focus();
  }, []);
  async function navigate(path?: string) {
    try {
      const row = await api<Directory>(
        `/api/workspace/directories${path ? `?path=${encodeURIComponent(path)}` : ""}`,
      );
      setDirectory(row);
      setFolder(row.path || "");
      if (!row.path && !row.directories.length) setMode("snapshot");
      if (row.path)
        setName(row.path.replace(/\\/g, "/").split("/").at(-1) || "");
    } catch (e) {
      setError((e as Error).message);
    }
  }
  async function select(list: FileList | null) {
    if (!list?.length) return;
    const selected = await readSelection(list);
    setFiles(selected.accepted);
    setSkipped(selected.skipped.length);
    const title = list[0].webkitRelativePath.split("/")[0];
    setFolder(title || list[0].name);
    if (!name) setName(title || "导入项目");
  }
  async function create() {
    if (!name.trim()) return;
    setBusy(true);
    setError("");
    try {
      const project = await post<Project>("/api/projects", {
        name,
        root_path: mode === "local" ? folder : null,
        folder_name: mode === "snapshot" ? folder : "",
      });
      created(project, files);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div
      className="modal-backdrop"
      onKeyDown={(e) => {
        if (e.key === "Escape") close();
        if (e.key === "Tab") {
          const items = dialog.current?.querySelectorAll<HTMLElement>(
            "button:not(:disabled), input:not([hidden]), select, a[href]",
          );
          if (!items?.length) return;
          if (e.shiftKey && document.activeElement === items[0]) {
            e.preventDefault();
            items[items.length - 1].focus();
          } else if (
            !e.shiftKey &&
            document.activeElement === items[items.length - 1]
          ) {
            e.preventDefault();
            items[0].focus();
          }
        }
      }}
    >
      <section
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby="project-dialog-title"
        className="project-dialog"
      >
        <div className="drawer-heading">
          <h2 id="project-dialog-title">添加项目</h2>
          <button className="icon-button" aria-label="关闭" onClick={close}>
            <X size={18} />
          </button>
        </div>
        <div className="dialog-body">
          <div className="segmented">
            <button
              className={mode === "local" ? "active" : ""}
              onClick={() => {
                setMode("local");
                setFolder(directory?.path || "");
                setFiles([]);
              }}
            >
              <FolderOpen size={15} />
              本地文件夹
            </button>
            <button
              className={mode === "snapshot" ? "active" : ""}
              onClick={() => {
                setMode("snapshot");
                setFolder("");
              }}
            >
              <Upload size={15} />
              导入文件夹
            </button>
          </div>
          {mode === "local" ? (
            <>
              <div className="folder-address">
                <button
                  className="icon-button"
                  aria-label="上一级文件夹"
                  disabled={!directory?.path}
                  onClick={() => void navigate(directory?.parent || undefined)}
                >
                  <ArrowLeft size={15} />
                </button>
                <span>{directory?.path || "选择工作目录"}</span>
              </div>
              <div className="folder-picker">
                {directory?.directories.map((d) => (
                  <button
                    key={d.path}
                    onClick={() => {
                      setName("");
                      void navigate(d.path);
                    }}
                  >
                    <Folder size={17} />
                    {d.name}
                  </button>
                ))}
                {directory && !directory.directories.length && (
                  <p className="muted">当前文件夹没有可浏览的子目录。</p>
                )}
              </div>
              <p className="muted">
                关联后可读取项目文件；导入资料时才建立长期索引。
              </p>
            </>
          ) : (
            <>
              <input
                ref={(node) => {
                  picker.current = node;
                  node?.setAttribute("webkitdirectory", "");
                }}
                type="file"
                multiple
                hidden
                aria-label="选择项目文件夹"
                onChange={(e) => void select(e.target.files)}
              />
              <button
                className="upload-zone"
                onClick={() => picker.current?.click()}
              >
                <FolderOpen size={28} />
                <b>{folder || "选择项目文件夹"}</b>
                <span>浏览器只导入快照，不持续访问磁盘</span>
              </button>
              {folder && (
                <p className="muted">
                  可导入 {files.length} 个文本文件，已过滤 {skipped}{" "}
                  个文件。创建后可预览并确认导入。
                </p>
              )}
            </>
          )}
          <label htmlFor="project-name">项目名称</label>
          <input
            id="project-name"
            autoFocus
            value={name}
            maxLength={160}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如 Si-agent"
          />
          {error && (
            <p role="alert" className="notice">
              {error}
            </p>
          )}
          <div className="dialog-actions">
            <button className="secondary" onClick={close}>
              取消
            </button>
            <button
              disabled={busy || !folder || !name.trim()}
              onClick={() => void create()}
            >
              {busy ? "正在创建…" : "创建项目"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
