import { uiMessage } from "./ui";
import Markdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
export type Project = {
  id: string;
  name: string;
  description: string;
  root_path?: string | null;
  folder_name?: string;
};
export type Thread = { id: string; title: string };
export type Model = {
  ref: { provider: string; model: string };
  capabilities: { tool_calling: boolean; streaming?: boolean };
};
export type ProjectNode = {
  id: string;
  uri: string;
  parent_uri: string | null;
  kind: string;
  title: string;
  abstract: string;
  overview: string;
  content?: string;
  revision: number;
  representation_revision: number;
  status: string;
  needs_review: boolean;
  source_event_ids: string[];
};
export type Evidence = {
  id: string;
  node_id: string;
  revision: number;
  quote: string;
  start_char: number;
  end_char: number;
  redacted: boolean;
};
export type Job = {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  error: string | null;
  result: Record<string, unknown>;
};
export type Candidate = {
  id: string;
  uri: string;
  kind: string;
  title: string;
  content: string;
  action: string;
  status: string;
  reason: string | null;
  expected_revision: number | null;
  supersedes_id: string | null;
  evidence_ids: string[];
  job_id: string | null;
  node_id: string | null;
};
export type Operation = {
  id: string;
  tool: string;
  status: string;
  arguments: Record<string, unknown>;
  result: Record<string, unknown>;
};
export type Run = {
  id: string;
  status: string;
  model: string;
  trace_id: string;
  reply?: string;
  error_code?: string;
  iterations?: number;
  charged_tokens?: number;
  usage_tokens?: number;
  operations?: Operation[];
};
export type RunEvent = {
  id: number;
  kind: string;
  data: Record<string, unknown>;
  created_at?: string;
};
export const terminal = (status: string) =>
  ["succeeded", "failed", "cancelled", "unknown"].includes(status);

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  const value = await response.json();
  if (!response.ok)
    throw new Error(
      typeof value.detail === "string"
        ? uiMessage(value.detail)
        : "请检查输入后重试。",
    );
  return value;
}
export const post = <T,>(path: string, body: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });

export function Citations({
  text,
  open,
}: {
  text: string;
  open: (uri: string) => void;
}) {
  const source = text.replace(
    /\[(si:\/\/[^\]\s]+)\](?!\()/g,
    (_, uri: string) => `[${uri.split("/").pop() || "来源"}](${uri})`,
  );
  return (
    <Markdown
      remarkPlugins={[remarkGfm]}
      urlTransform={(url) =>
        url.startsWith("si://") ? url : defaultUrlTransform(url)
      }
      components={{
        a: ({ href, children }) =>
          href?.startsWith("si://") ? (
            <button
              className="citation"
              title={href}
              onClick={() => open(href)}
            >
              {children}
            </button>
          ) : (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
      }}
    >
      {source}
    </Markdown>
  );
}
