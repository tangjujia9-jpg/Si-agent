import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { App } from "./App";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

test("a completed import refreshes the tree and opens source evidence", async () => {
  let imported = false;
  const node = {
    id: "node-1",
    uri: "si://users/default/projects/p/resources/design.md",
    parent_uri: "si://users/default/projects/p/resources",
    kind: "resource",
    title: "design.md",
    abstract: "A source",
    overview: "A source",
    revision: 1,
    source_event_ids: ["job-1"],
  };
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, init?: RequestInit) => {
      let value: unknown;
      if (path === "/api/projects")
        value = [{ id: "p", name: "Demo", description: "" }];
      else if (path === "/api/system")
        value = { database: "postgresql", vector_version: "0.8.2" };
      else if (path.endsWith("/ingest") && init?.method === "POST") {
        imported = true;
        value = { id: "job-1", status: "queued", attempts: 0, error: null };
      } else if (path === "/api/jobs/job-1")
        value = { id: "job-1", status: "succeeded", attempts: 1, error: null };
      else if (path.endsWith("/context/tree")) value = imported ? [node] : [];
      else if (path === "/api/memory/node-1")
        value = { ...node, content: "The original source evidence." };
      else if (path === "/api/models") value = [];
      else if (
        path.endsWith("/threads") ||
        path.endsWith("/evidence") ||
        path.endsWith("/versions") ||
        path.includes("/evidence?")
      )
        value = [];
      else throw new Error(`Unexpected request ${path}`);
      return { ok: true, json: async () => value };
    }),
  );
  render(<App />);
  await waitFor(() =>
    expect(
      (screen.getByRole("button", { name: "项目资料" }) as HTMLButtonElement)
        .disabled,
    ).toBe(false),
  );
  fireEvent.click(screen.getByRole("button", { name: "项目资料" }));
  const file = new File(["Document text"], "design.md", {
    type: "text/markdown",
  });
  Object.defineProperty(file, "arrayBuffer", {
    value: async () => new TextEncoder().encode("Document text").buffer,
  });
  fireEvent.change(screen.getByLabelText("上传文件", { selector: "input" }), {
    target: { files: [file] },
  });
  fireEvent.click(await screen.findByRole("button", { name: "导入所选资料" }));
  const rows = await screen.findAllByRole(
    "button",
    { name: /design.md/ },
    { timeout: 4000 },
  );
  fireEvent.click(rows[0]);
  await screen.findByText("The original source evidence.");
  expect(screen.getByText(node.uri).textContent).toBe(node.uri);
  expect(
    screen.getByRole("link", { name: "项目监控" }).getAttribute("target"),
  ).toBe("_blank");
  await waitFor(() => expect(screen.getByLabelText("来源详情")).toBeTruthy());
});
