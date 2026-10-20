export type SourceFile = {
  path: string;
  size: number;
  content?: string;
  file?: File;
};
const extensions =
  /\.(md|txt|py|ts|tsx|js|jsx|json|css|html|sql|yaml|yml|toml|rs|go|java|cpp|c|h|sh)$/i;
const excluded =
  /(^|\/)(\.[^/]+|node_modules|venv|__pycache__|dist|build|hosted)(\/|$)|credential|secret|token|private_key|id_rsa/i;
export async function readSelection(list: FileList | File[]) {
  const accepted: SourceFile[] = [],
    skipped: string[] = [];
  for (const file of Array.from(list)) {
    const relative = file.webkitRelativePath || file.name;
    const path = file.webkitRelativePath
      ? relative.split("/").slice(1).join("/")
      : relative;
    if (
      accepted.some((f) => f.path === path) ||
      accepted.length >= 500 ||
      excluded.test(path) ||
      !extensions.test(path) ||
      file.size > 200000 ||
      !file.size ||
      path.length > 240 ||
      path.split("/").some((p) => p === ".." || p === ".") ||
      /[:%?#\\]/.test(path)
    ) {
      skipped.push(relative);
      continue;
    }
    accepted.push({ path, size: file.size, file });
  }
  return { accepted, skipped };
}
export async function fileText(item: SourceFile) {
  if (item.content !== undefined) return item.content;
  if (!item.file) throw new Error("无法读取文件。");
  const buffer = await item.file.arrayBuffer();
  const text = new TextDecoder("utf-8", { fatal: true }).decode(buffer);
  if (!text.trim() || text.includes("\0"))
    throw new Error(`${item.path} 不是有效的 UTF-8 文本。`);
  return text;
}
export function batches(files: { path: string; content: string }[]) {
  const result: (typeof files)[] = [];
  let batch: typeof files = [],
    size = 0;
  for (const file of files) {
    const bytes = new TextEncoder().encode(file.content).length;
    if (batch.length >= 50 || size + bytes > 1900000) {
      result.push(batch);
      batch = [];
      size = 0;
    }
    batch.push(file);
    size += bytes;
  }
  if (batch.length) result.push(batch);
  return result;
}
