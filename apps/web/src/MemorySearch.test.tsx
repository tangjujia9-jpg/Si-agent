import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { MemorySearch } from './MemorySearch';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

test('search displays citations and invokes the source inspector', async () => {
  const inspect = vi.fn();
  const uri = 'si://users/default/projects/p/resources/design.md';
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    expect(url).toContain('project_id=p'); expect(url).toContain('q=storage');
    return { ok: true, json: async () => ({ hits: [{ id: 'n', uri, tier: 'l2', snippet: 'Use pgvector.', evidence_ids: ['import-1'] }],
      context: `[${uri}]\nUse pgvector.`, estimated_tokens: 100, truncated: false, embedding_model: null,
      warnings: ['Embeddings disabled; lexical retrieval was used.'], stages: [] }) };
  }));
  render(<MemorySearch projectId="p" inspect={inspect} />);
  fireEvent.change(screen.getByLabelText('SEARCH THIS PROJECT'), { target: { value: 'storage' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search evidence ↗' }));
  await screen.findByText('Use pgvector.');
  expect(screen.getByText(uri)).toBeTruthy();
  fireEvent.click(screen.getByRole('button', { name: 'Open source ↗' }));
  expect(inspect).toHaveBeenCalledWith('n');
});

test('switching projects cancels pending retrieval and discards its result', async () => {
  let resolve: (value: unknown) => void = () => {};
  vi.stubGlobal('fetch', vi.fn(() => new Promise(r => { resolve = r; })));
  const view = render(<MemorySearch projectId="a" inspect={() => {}} />);
  fireEvent.change(screen.getByLabelText('SEARCH THIS PROJECT'), { target: { value: 'storage' } });
  fireEvent.click(screen.getByRole('button', { name: 'Search evidence ↗' }));
  view.rerender(<MemorySearch projectId="b" inspect={() => {}} />);
  await act(async () => { resolve({ ok: true, json: async () => ({ hits: [], warnings: [], stages: [], estimated_tokens: 1 }) }); });
  expect(screen.queryByText('0 evidence sources')).toBeNull();
  expect((screen.getByRole('button', { name: 'Search evidence ↗' }) as HTMLButtonElement).disabled).toBe(false);
});
