import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { App } from './App';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('a completed import refreshes the tree and opens source evidence', async () => {
  let imported = false;
  const node = { id: 'node-1', uri: 'si://users/default/projects/p/resources/design.md',
    parent_uri: 'si://users/default/projects/p/resources', kind: 'resource', title: 'design.md',
    abstract: 'A source', overview: 'A source', revision: 1, source_event_ids: ['job-1'] };
  vi.stubGlobal('fetch', vi.fn(async (path: string, init?: RequestInit) => {
    let value: unknown;
    if (path === '/api/projects') value = [{ id: 'p', name: 'Demo', description: '' }];
    else if (path === '/ready') value = { status: 'ready' };
    else if (path.endsWith('/ingest') && init?.method === 'POST') {
      imported = true; value = { id: 'job-1', status: 'queued', attempts: 0, error: null };
    } else if (path === '/api/jobs/job-1') value = { id: 'job-1', status: 'succeeded', attempts: 1, error: null };
    else if (path.endsWith('/context/tree')) value = imported ? [node] : [];
    else if (path === '/api/memory/node-1') value = { ...node, content: 'The original source evidence.' };
    else if (path === '/api/models') value = [];
    else if (path.endsWith('/threads') || path.endsWith('/evidence') || path.endsWith('/versions') || path.includes('/evidence?')) value = [];
    else throw new Error(`Unexpected request ${path}`);
    return { ok: true, json: async () => value };
  }));
  render(<App />);
  await screen.findByRole('tab', { name: '上下文浏览' });
  fireEvent.click(screen.getByRole('tab', { name: '上下文浏览' }));
  await screen.findByText('导入第一份资料后，这里会显示上下文目录。');
  fireEvent.change(screen.getByLabelText('资料正文'), { target: { value: 'Document text' } });
  fireEvent.click(screen.getByRole('button', { name: '导入文档 ↗' }));
  const row = await screen.findByRole('button', { name: /design.md/ }, { timeout: 4000 });
  fireEvent.click(row);
  await screen.findByText('The original source evidence.');
  expect(screen.getByText(node.uri).textContent).toBe(node.uri);
  await waitFor(() => expect((screen.getByRole('button', { name: '导入文档 ↗' }) as HTMLButtonElement).disabled).toBe(false));
});
