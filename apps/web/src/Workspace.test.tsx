import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { useRunStream } from "./useRunStream";
import { Citations } from "./api";
import { batches, readSelection } from "./files";
import { Monitor } from "./Monitor";
import { Chat } from "./Chat";
import { Sources } from "./Sources";
import { traceLayout } from "./RunTrace";
import { RunActivity } from "./RunActivity";
import type { Run } from "./api";

afterEach(() => {
  cleanup();
  localStorage.clear();
  vi.unstubAllGlobals();
});
const response = (value: unknown) => ({ ok: true, json: async () => value });
class Stream {
  static current: Stream;
  listeners = new Map<string, (e: unknown) => void>();
  close = vi.fn();
  constructor(public url: string) {
    Stream.current = this;
  }
  addEventListener(kind: string, callback: (e: unknown) => void) {
    this.listeners.set(kind, callback);
  }
  emit(kind: string, data: unknown, id = "") {
    this.listeners.get(kind)?.({ data: JSON.stringify(data), lastEventId: id });
  }
}
function Live() {
  const live = useRunStream("r", () => {});
  return <div aria-label="output">{live.text}</div>;
}

test("activity explains the local gate and distinguishes cumulative budget failure", () => {
  const run = { id: "r", status: "failed", iterations: 2, error_code: "token_budget" } as Run;
  render(<RunActivity run={run} events={[
    { id: 1, kind: "retrieval_decision", data: { source: "local-rule", retrieve: false } },
  ]} refresh={() => {}} />);
  expect(screen.getByText(/累计预算已耗尽/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: /失败/ }));
  expect(screen.getByText("检索门 · 本地规则 · 跳过")).toBeTruthy();
});
test("text streams before completion and replay is deduplicated across tool turns", async () => {
  vi.stubGlobal("EventSource", Stream);
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) =>
      response(path.includes("/events?") ? [] : { id: "r", status: "running" }),
    ),
  );
  render(<Live />);
  await waitFor(() => expect(Stream.current).toBeTruthy());
  act(() => {
    Stream.current.emit("llm_delta", { text: "正在读文件" }, "1");
    Stream.current.emit("llm_delta", { text: "重复" }, "1");
  });
  expect(screen.getByLabelText("output").textContent).toBe("正在读文件");
  act(() => {
    Stream.current.emit("llm_stream_start", {}, "2");
    Stream.current.emit("llm_delta", { text: "根据文件得出结论" }, "3");
  });
  expect(screen.getByLabelText("output").textContent).toBe("根据文件得出结论");
});
test("source links remain usable while raw HTML and unsafe URLs cannot execute", () => {
  const open = vi.fn();
  const { container } = render(
    <Citations
      text={
        "## 分析\n\n[si://users/me/projects/p/resources/design.md]\n\n[bad](javascript:alert(1)) <img src=x onerror=alert(1)>"
      }
      open={open}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "design.md" }));
  expect(open).toHaveBeenCalledWith(
    "si://users/me/projects/p/resources/design.md",
  );
  expect(container.querySelector("img")).toBe(null);
  expect(container.querySelector('a[href^="javascript:"]')).toBe(null);
});
test("folder selection preserves relative paths and excludes secrets, generated files and duplicates", async () => {
  const names = [
    "docs/design.md",
    ".env.json",
    "api-token.txt",
    "node_modules/test.js",
    "docs/design.md",
  ];
  const files = names.map((name) => {
    const f = new File(["text"], name.split("/").at(-1)!);
    Object.defineProperty(f, "webkitRelativePath", {
      value: `project/${name}`,
    });
    return f;
  });
  const result = await readSelection(files);
  expect(result.accepted.map((f) => f.path)).toEqual(["docs/design.md"]);
  expect(result.skipped.length).toBe(4);
  expect(
    batches(
      Array.from({ length: 51 }, (_, n) => ({
        path: `${n}.md`,
        content: "文",
      })),
    ).map((b) => b.length),
  ).toEqual([50, 1]);
});
test("monitor loads the first project when the async project catalog arrives", async () => {
  const fetcher = vi.fn(async () => response({ runs: [], jobs: [] }));
  vi.stubGlobal("fetch", fetcher);
  const { rerender } = render(<Monitor projects={[]} initial="" />);
  rerender(
    <Monitor
      projects={[{ id: "p", name: "Project", description: "" }]}
      initial=""
    />,
  );
  await waitFor(() =>
    expect(fetcher).toHaveBeenCalledWith(
      "/api/projects/p/monitor",
      expect.any(Object),
    ),
  );
});
test("the first message retries in the same thread after an ambiguous connection failure", async () => {
  const bodies: Record<string, unknown>[] = [];
  let threads = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      if (path === "/api/threads") {
        threads++;
        return response({ id: "t", title: "New" });
      }
      if (init?.method === "POST") {
        bodies.push(JSON.parse(String(init.body)));
        throw new Error("Connection lost");
      }
      return response(
        path === "/api/models"
          ? [
              {
                ref: { provider: "demo", model: "demo" },
                capabilities: { tool_calling: true },
              },
            ]
          : [],
      );
    }),
  );
  render(<Chat projectId="p" threadId="" open={() => {}} changed={() => {}} />);
  fireEvent.change(screen.getByLabelText("消息内容"), {
    target: { value: "Read this project" },
  });
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "发送消息" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await screen.findByText("Connection lost");
  fireEvent.click(screen.getByRole("button", { name: "发送消息" }));
  await waitFor(() => expect(bodies.length).toBe(2));
  expect(threads).toBe(1);
  expect(bodies[0].idempotency_key).toBe(bodies[1].idempotency_key);
  expect(bodies[0]).not.toHaveProperty("thread");
});
test("an ambiguous import retry reuses the batch idempotency key", async () => {
  const keys: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_path: string, init?: RequestInit) => {
      keys.push(JSON.parse(String(init?.body)).idempotency_key);
      throw new Error("Connection lost");
    }),
  );
  render(
    <Sources
      project={{ id: "p", name: "Project", description: "" }}
      nodes={[]}
      initial={[{ path: "design.md", content: "Project facts", size: 13 }]}
      inspect={() => {}}
      changed={() => {}}
    />,
  );
  fireEvent.click(screen.getByRole("button", { name: "导入所选资料" }));
  await screen.findByText("Connection lost");
  fireEvent.click(screen.getByRole("button", { name: "导入所选资料" }));
  await waitFor(() => expect(keys.length).toBe(2));
  expect(keys[0]).toBe(keys[1]);
});

test("trace timeline preserves parent depth and actual start offsets", () => {
  const records = [
    {
      id: 2,
      kind: "span",
      data: {
        span_id: "child",
        parent_id: "root",
        started_at: "2026-10-20T10:00:00.050Z",
        ended_at: "2026-10-20T10:00:00.075Z",
        duration_ms: 25,
      },
    },
    {
      id: 3,
      kind: "span",
      data: {
        span_id: "root",
        parent_id: null,
        started_at: "2026-10-20T10:00:00.000Z",
        ended_at: "2026-10-20T10:00:00.100Z",
        duration_ms: 100,
      },
    },
  ];
  const rows = traceLayout(records);
  expect(rows[0].event.data.span_id).toBe("root");
  expect(rows[1].depth).toBe(1);
  expect(rows[1].offset).toBe(50);
  expect(rows[1].width).toBe(25);
});
