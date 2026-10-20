import { useEffect, useRef, useState } from "react";
import { api, terminal, type Run, type RunEvent } from "./api";

const kinds = [
  "run_queued",
  "run_start",
  "span",
  "memory_search",
  "llm_start",
  "llm_stream_start",
  "llm_delta",
  "llm_end",
  "tool_start",
  "tool_end",
  "approval_required",
  "approval",
  "run_end",
  "run_pause",
  "run_error",
  "memory_write",
  "decision_start",
  "embedding_start",
  "retrieval_decision",
  "context_plan",
  "context_recovery",
];
export function useRunStream(runId: string, onFinish: () => void) {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [connection, setConnection] = useState("正在连接");
  const [error, setError] = useState("");
  const finish = useRef(onFinish);
  finish.current = onFinish;
  useEffect(() => {
    if (!runId) {
      setRun(null);
      setEvents([]);
      return;
    }
    let active = true,
      cursor = 0,
      notified = false;
    let source: EventSource | undefined;
    const abort = new AbortController();
    setEvents([]);
    setRun(null);
    setError("");
    setConnection("正在连接");
    const update = (next: Run) => {
      if (!active) return;
      setRun(next);
      if (terminal(next.status)) {
        source?.close();
        setConnection("历史记录");
        if (!notified) {
          notified = true;
          finish.current();
        }
      }
    };
    async function connect() {
      const initial: RunEvent[] = [];
      while (active) {
        const page = await api<RunEvent[]>(
          `/api/runs/${runId}/events?after=${cursor}`,
          { signal: abort.signal },
        );
        initial.push(...page);
        cursor = page.at(-1)?.id ?? cursor;
        if (page.length < 500) break;
      }
      const snapshot = await api<Run>(`/api/runs/${runId}`, {
        signal: abort.signal,
      });
      if (!active) return;
      setEvents(initial);
      update(snapshot);
      if (terminal(snapshot.status)) return;
      source = new EventSource(
        `/api/runs/${runId}/events/stream?after=${cursor}`,
      );
      source.onopen = () => {
        if (active) setConnection("实时更新");
      };
      source.onerror = () => {
        if (active) setConnection("正在重连 · 已保留事件记录");
      };
      for (const kind of kinds)
        source.addEventListener(kind, (event) => {
          if (!active) return;
          const message = event as MessageEvent,
            id = Number(message.lastEventId);
          if (!id || id <= cursor) return;
          try {
            const data = JSON.parse(message.data);
            cursor = id;
            setEvents((rows) => [...rows, { id, kind, data }]);
          } catch {
            setError("无法读取运行事件，请重新打开会话。");
          }
        });
      source.addEventListener("snapshot", (event) => {
        try {
          update(JSON.parse((event as MessageEvent).data));
        } catch {
          if (active) setError("暂时无法读取运行状态。");
        }
      });
    }
    void connect().catch((e) => {
      if (active) setError(e.message);
    });
    return () => {
      active = false;
      abort.abort();
      source?.close();
    };
  }, [runId]);
  let text = "";
  for (const event of events) {
    if (event.kind === "llm_stream_start") text = "";
    if (event.kind === "llm_delta" && !event.data.redacted)
      text += String(event.data.text || "");
  }
  return { run, events, connection, error, text, setRun };
}
