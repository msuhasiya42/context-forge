import { act, cleanup, screen, waitFor } from '@testing-library/react';
import { createMockClient } from '../api/client';
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
