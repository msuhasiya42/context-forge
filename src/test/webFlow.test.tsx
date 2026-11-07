import { act, cleanup, screen, waitFor, within } from '@testing-library/react';
import { createMockClient } from '../api/client';
import { parseMessage } from '../api/parse';
import type { StreamEvent } from '../api/types';
import { agentText, chat, renderApp, userRequest } from './fixtures';

it('streams an answer and closes the stream when the view is unmounted', async () => {
  const request = {
    ...userRequest('r1', 'c1', 'Research Python', '2026-11-07T10:00:00Z'),
    status: 'RUNNING' as const,
  };
  const client = createMockClient({
    seed: {
      chats: [chat('c1', 'Research', '2026-11-07T10:00:00Z')],
      messages: [request],
    },
  });
  let emit: ((event: StreamEvent) => void) | undefined;
  const close = vi.fn();
  client.streamRequest = vi.fn((_chat, _request, onEvent) => {
    emit = onEvent;
    return close;
  });
  renderApp(client);
  await screen.findByText('Research Python');
  await waitFor(() => expect(emit).toBeDefined());
  await act(async () => {
    emit?.({ kind: 'activity', text: 'Calling web_search' });
    emit?.({
      kind: 'message',
      message: agentText('answer', 'c1', 'Python sources found.', '2026-11-07T10:01:00Z'),
    });
  });
  expect(await screen.findByText('Python sources found.')).toBeInTheDocument();
  expect(screen.getByText('Calling web_search')).toBeInTheDocument();
  cleanup();
  expect(close).toHaveBeenCalled();
});

it('sends clarification replies to the existing request and saves standing instructions', async () => {
  const request = {
    ...userRequest('r1', 'c1', 'Compare Python versions', '2026-11-07T10:00:00Z'),
    status: 'WAITING_USER' as const,
  };
  const question = {
    ...agentText('q1', 'c1', 'Which versions?', '2026-11-07T10:00:01Z'),
    kind: 'clarification' as const,
    requestMessageId: 'r1',
  };
  const client = createMockClient({
    seed: {
      chats: [chat('c1', 'Research', '2026-11-07T10:00:00Z')],
      messages: [request, question],
    },
  });
  client.replyToRequest = vi.fn(async (chatId, requestId, targetId, content) => ({
    ...userRequest('reply', chatId, content, '2026-11-07T10:00:02Z'),
    kind: 'clarification_response' as const,
    requestMessageId: requestId,
    inReplyToMessageId: targetId,
  }));
  client.getMemory = vi.fn(async () => ({ instructions: 'Be concise.', preferences: null }));
  client.saveMemory = vi.fn(async (instructions) => ({ instructions, preferences: null }));
  const { user } = renderApp(client);
  await screen.findByText('Which versions?');
  await user.type(screen.getByRole('textbox', { name: 'Message' }), '3.12 and 3.13');
  await user.click(screen.getByRole('button', { name: 'Send' }));
  await waitFor(() =>
    expect(client.replyToRequest).toHaveBeenCalledWith('c1', 'r1', 'q1', '3.12 and 3.13'),
  );
  await user.click(screen.getByText('Standing instructions'));
  const memory = screen.getByLabelText('Instructions to use across chats');
  await waitFor(() => expect(memory).toHaveValue('Be concise.'));
  await user.clear(memory);
  await user.type(memory, 'Prefer primary sources.');
  await user.click(screen.getByRole('button', { name: 'Save instructions' }));
  await waitFor(() => expect(client.saveMemory).toHaveBeenCalledWith('Prefer primary sources.'));
});

it('opens a long report in a side panel, renders its markdown safely and downloads it', async () => {
  // As the API sends it: the chat keeps a summary, report.deliverables names the full report.
  const report = parseMessage({
    messageId: 'rep1',
    chatId: 'c1',
    userId: 'u1',
    sender: 'assistant',
    kind: 'report',
    content: 'US stocks rose this week.\n\nFull report attached: 6 findings, 1 limitations.',
    requestMessageId: 'r1',
    createdAt: '2026-11-07T10:02:00Z',
    updatedAt: '2026-11-07T10:02:00Z',
    report: {
      claims: [],
      inputsConsulted: [],
      deliverables: [{ artifactId: 'art1', role: 'output' }],
      uncertainties: [],
      coverage: {},
    },
  });
  const client = createMockClient({
    seed: {
      chats: [chat('c1', 'Markets', '2026-11-07T10:02:00Z')],
      messages: [userRequest('r1', 'c1', 'Compare markets', '2026-11-07T10:00:00Z'), report],
    },
  });
  const markdown =
    '# Markets this week\n\nThe **S&P 500** rose. [Reuters](https://example.com/r)\n\n' +
    '<script>alert(1)</script><img src="x" onerror="alert(1)">\n\n[bad](javascript:alert(1))';
  client.getArtifactContent = vi.fn(async () => markdown);
  const saved: Blob[] = [];
  const createObjectURL = vi.fn((blob: Blob) => {
    saved.push(blob);
    return 'blob:report';
  });
  Object.assign(URL, { createObjectURL, revokeObjectURL: vi.fn() });
  const { user } = renderApp(client);

  const open = await screen.findByRole('button', { name: 'View full report' });
  await user.click(open);
  const panel = await screen.findByRole('dialog', { name: 'Full report' });
  expect(await within(panel).findByRole('heading', { name: 'Markets this week' })).toBeVisible();
  expect(client.getArtifactContent).toHaveBeenCalledWith('c1', 'art1');
  expect(within(panel).getByRole('link', { name: 'Reuters' })).toHaveAttribute('target', '_blank');
  expect(panel.querySelector('script, img, a[href^="javascript"]')).toBeNull();
  expect(within(panel).getByText('bad')).toBeInTheDocument();

  await user.click(within(panel).getByRole('button', { name: 'Download' }));
  expect(createObjectURL).toHaveBeenCalledOnce();
  expect(await saved[0]?.text()).toBe(markdown);
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(open).toHaveFocus();
});
