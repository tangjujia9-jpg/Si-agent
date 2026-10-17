import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { Chat } from './Chat';
import { RunTrace } from './RunTrace';
import { MemoryInspector } from './MemoryInspector';
import { MemoryReview } from './MemoryReview';
import type { ProjectNode } from './api';

afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });
const response = (value: unknown, ok = true) => ({ ok, json: async () => value });
const node: ProjectNode = { id: 'n', uri: 'si://users/default/projects/p/memories/storage.md', parent_uri: null,
  kind: 'semantic', title: 'Storage', abstract: 'Database', overview: 'Database decision', content: 'Use SQLite.',
  revision: 1, representation_revision: 2, status: 'active', needs_review: false, source_event_ids: [] };

class Stream {
  static instances: Stream[] = [];
  url: string; onerror?: () => void; onopen?: () => void;
  listeners = new Map<string, (event: unknown) => void>();
  close = vi.fn();
  constructor(url: string) { this.url = url; Stream.instances.push(this); }
  addEventListener(name: string, callback: (event: unknown) => void) { this.listeners.set(name, callback); }
  emit(type: string, data: unknown, id = '') { this.listeners.get(type)?.({ type, data: JSON.stringify(data), lastEventId: id }); }
}

test('run trace resumes the durable cursor, deduplicates replay and closes after completion', async () => {
  Stream.instances = []; vi.stubGlobal('EventSource', Stream);
  vi.stubGlobal('fetch', vi.fn(async (path: string) => response(path.includes('/events?')
    ? [{ id: 1, kind: 'run_start', data: {} }]
    : { id: 'r', status: 'running', model: 'demo', trace_id: 'trace' })));
  const finish = vi.fn(); render(<RunTrace runId="r" onFinish={finish} />);
  await waitFor(() => expect(Stream.instances.length).toBe(1));
  const source = Stream.instances[0]; expect(source.url).toContain('after=1');
  act(() => { source.emit('tool_start', { tool: 'search_memory' }, '2'); source.emit('tool_start', {}, '2'); source.onerror?.(); });
  expect(screen.getByText('正在重连 · 已保留事件记录')).toBeTruthy();
  expect(screen.getAllByText('工具开始').length).toBe(1);
  act(() => source.emit('snapshot', { id: 'r', status: 'succeeded', model: 'demo', trace_id: 'trace' }));
  expect(finish).toHaveBeenCalledTimes(1); expect(source.close).toHaveBeenCalled();
});

test('a pending operation can be rejected without reporting it as completed', async () => {
  vi.stubGlobal('EventSource', Stream);
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => response(path.includes('/events?') ? []
    : init?.method === 'POST' ? { status: 'rejected' }
      : { id: 'r', status: 'waiting_approval', model: 'demo', operations: [{ id: 'o', tool: 'forget_memory', status: 'pending', arguments: { uri: node.uri }, result: {} }] }));
  vi.stubGlobal('fetch', fetcher);
  render(<RunTrace runId="r" onFinish={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '拒绝操作' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([path, init]) => path === '/api/operations/o/approval' && JSON.parse(String(init?.body)).approved === false)).toBe(true));
});

test('chat reloads the chosen conversation and follows a source citation', async () => {
  localStorage.setItem('si-thread:p', 't');
  const open = vi.fn();
  vi.stubGlobal('fetch', vi.fn(async (path: string) => response(path.endsWith('/threads') ? [{ id: 't', title: 'Saved conversation' }]
    : path === '/api/models' ? [{ ref: { provider: 'demo', model: 'evidence-demo' }, capabilities: { tool_calling: true } }]
      : path.endsWith('/messages') ? [{ id: 'm', sequence: 1, role: 'assistant', content: `Storage [${node.uri}]`, event_data: {} }]
        : [])));
  render(<Chat projectId="p" open={open} changed={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'storage.md' }));
  expect(open).toHaveBeenCalledWith(node.uri);
});

test('chat retries an ambiguous transport failure with the same idempotency key', async () => {
  let attempts = 0; const bodies: Record<string, unknown>[] = [];
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    if (path.endsWith('/messages') && init?.method === 'POST') {
      bodies.push(JSON.parse(String(init.body))); attempts++; throw new Error('Connection lost');
    }
    return response(path.endsWith('/threads') ? [{ id: 't', title: 'Saved conversation' }] : path === '/api/models'
      ? [{ ref: { provider: 'demo', model: 'evidence-demo', kind: 'chat' }, capabilities: { tool_calling: true } }] : []);
  }));
  render(<Chat projectId="p" open={() => {}} changed={() => {}} />);
  await waitFor(() => expect((screen.getByRole('button', { name: '发送消息 ↗' }) as HTMLButtonElement).disabled).toBe(false));
  fireEvent.change(screen.getByLabelText('消息内容'), { target: { value: 'What storage?' } });
  fireEvent.click(screen.getByRole('button', { name: '发送消息 ↗' }));
  await screen.findByText('Connection lost');
  fireEvent.click(screen.getByRole('button', { name: '发送消息 ↗' }));
  await waitFor(() => expect(attempts).toBe(2));
  expect(bodies[0].idempotency_key).toBe(bodies[1].idempotency_key);
  expect(bodies[0]).not.toHaveProperty('kind');
});

test('forget requires an impact preview and explicit acknowledgement', async () => {
  const fetcher = vi.fn(async (path: string) => response(path.endsWith('/versions') || path.endsWith('/evidence') ? []
    : path.endsWith('/forget-preview') ? { derived_node_ids: ['derived'], redacts_messages: true } : path.endsWith('/forget') ? { forgotten: true } : node));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryInspector nodeId="n" projectId="p" revision={0} changed={() => {}} open={() => {}} />);
  await screen.findByRole('heading', { name: 'Storage' });
  fireEvent.click(screen.getByText('忘记此来源'));
  fireEvent.click(screen.getByRole('button', { name: '预览影响范围' }));
  const forget = await screen.findByRole('button', { name: '确认忘记' });
  expect((forget as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByLabelText('我已知晓来源历史将被脱敏'));
  fireEvent.click(forget);
  await waitFor(() => expect(fetcher.mock.calls.some(([path]) => path === '/api/memory/n/forget')).toBe(true));
});

test('review can skip a pending proposal and inspect its source evidence', async () => {
  const c = { id: 'c', title: 'Storage', uri: node.uri, status: 'pending', action: 'UPDATE', content: 'Use PostgreSQL.', expected_revision: 1, job_id: null, evidence_ids: ['e'] };
  const fetcher = vi.fn(async (path: string, init?: RequestInit) => response(path.endsWith('/c/skip') ? { ...c, status: 'skipped' }
    : path.includes('/evidence?') ? [{ id: 'e', node_id: 'source', quote: 'Use PostgreSQL.' }]
      : path.endsWith('/candidates') ? [c] : init?.method ? {} : []));
  vi.stubGlobal('fetch', fetcher);
  render(<MemoryReview projectId="p" nodes={[]} changed={() => {}} open={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看证据（1）' }));
  await screen.findByRole('button', { name: '打开来源 ↗' });
  fireEvent.click(screen.getByRole('button', { name: '跳过候选' }));
  await waitFor(() => expect(fetcher.mock.calls.some(([path]) => path === '/api/memory/candidates/c/skip')).toBe(true));
});
