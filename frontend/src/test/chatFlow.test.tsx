import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { createHttpClient, createMockClient, type ApiClient } from '../api/client';
import { ApiError } from '../api/errors';
import type { Message } from '../api/types';
import { agentText, deferred, renderApp, twoChatSeed } from './fixtures';

const messageLog = () => screen.findByRole('log', { name: 'Messages' });
const messageInput = () => screen.getByRole('textbox', { name: 'Message' });

describe('chat flow', () => {
  it('shows a chat, sends a message with Enter and shows the agent reply', async () => {
    const { user } = renderApp(createMockClient({ seed: twoChatSeed(), replyDelayMs: 20 }));

    await user.click(await screen.findByRole('button', { name: 'Travel' }));
    expect(await within(await messageLog()).findByText('Plan a trip')).toBeInTheDocument();

    await user.upload(
      screen.getByLabelText('Attach'),
      new File(['a,b'], 'october.csv', { type: 'text/csv' }),
    );
    expect(
      within(screen.getByRole('list', { name: 'Attached files' })).getByText('october.csv'),
    ).toBeInTheDocument();

    await user.type(messageInput(), 'Compare months{Shift>}{Enter}{/Shift}excluding transfers');
    expect(messageInput()).toHaveValue('Compare months\nexcluding transfers');
    await user.keyboard('{Enter}');

    const log = await messageLog();
    expect(
      await within(log).findByText('Compare months excluding transfers', {
        normalizer: (text) => text.replace(/\s+/g, ' '),
      }),
    ).toBeInTheDocument();
    expect(
      await within(log).findByText(/Echo from the mock agent: Compare months/),
    ).toBeInTheDocument();
    expect(messageInput()).toHaveValue('');
    expect(screen.queryByRole('list', { name: 'Attached files' })).not.toBeInTheDocument();
  });

  it("takes an untitled chat's name from the server after the first send", async () => {
    const base = createMockClient({ seed: { chats: [], messages: [] }, replyDelayMs: 5 });
    const renameChat = vi.fn(base.renameChat);
    const { user } = renderApp({ ...base, renameChat });

    await user.click(await screen.findByRole('button', { name: 'New chat' }));
    await screen.findByText('Start the conversation');
    await user.type(messageInput(), 'Card bills paid?{Enter}');

    await waitFor(() =>
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Card bills paid?'),
    );
    expect(renameChat).not.toHaveBeenCalled();
  });

  it('renders every message kind from the HTTP API and skips one it cannot read', async () => {
    vi.stubGlobal('fetch', fakeBackend(httpMessages));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    renderApp(createHttpClient('http://api.test'));

    const log = await messageLog();
    expect(await within(log).findByText('Archive 12 old statements?')).toBeInTheDocument();
    expect(within(log).getByText('Status: Awaiting your decision')).toBeInTheDocument();
    expect(within(log).getByText('Decision')).toBeInTheDocument();
    expect(within(log).getByText('Status: Failed')).toBeInTheDocument();
    expect(within(log).getByText('Reason: The file store was unavailable')).toBeInTheDocument();
    expect(within(log).queryByText('Unreadable')).not.toBeInTheDocument();
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it('keeps the messages and shows a notice when a background refresh fails', async () => {
    const seed = twoChatSeed();
    seed.messages = seed.messages.map((m) =>
      m.messageId === 'm-1' ? { ...m, status: 'RUNNING' } : m,
    );
    const base = createMockClient({ seed });
    let calls = 0;
    const client: ApiClient = {
      ...base,
      listMessages: (chatId) =>
        ++calls === 1
          ? base.listMessages(chatId)
          : Promise.reject(new ApiError('network', 'Server unreachable.')),
    };
    renderApp(client);

    expect(
      await screen.findByText(/Could not refresh messages: Server unreachable\./),
    ).toBeInTheDocument();
    expect(within(await messageLog()).getByText('You spent 1,200.')).toBeInTheDocument();
  });

  it('discards a stale message response after switching chats', async () => {
    const base = createMockClient({ seed: twoChatSeed() });
    const slowBudget = deferred<Message[]>();
    const client: ApiClient = {
      ...base,
      listMessages: (chatId) =>
        chatId === 'chat-budget' ? slowBudget.promise : base.listMessages(chatId),
    };
    const { user } = renderApp(client);

    expect(await screen.findByText('Loading messages…')).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: 'Travel' }));
    expect(await within(await messageLog()).findByText('Plan a trip')).toBeInTheDocument();

    slowBudget.resolve([
      agentText('m-stale', 'chat-budget', 'Stale budget reply', '2026-11-02T09:00:00Z'),
    ]);
    await slowBudget.promise;

    await waitFor(() => expect(screen.queryByText('Stale budget reply')).not.toBeInTheDocument());
    expect(within(await messageLog()).getByText('Plan a trip')).toBeInTheDocument();
  });

  it('shows a load error with Retry and keeps the draft when sending fails', async () => {
    const base = createMockClient({ seed: twoChatSeed() });
    let failLoad = true;
    const client: ApiClient = {
      ...base,
      listMessages: (chatId) =>
        failLoad
          ? Promise.reject(new ApiError('network', 'Could not reach the server.'))
          : base.listMessages(chatId),
      submitRequest: () => Promise.reject(new ApiError('validation', 'Message is too long.', 400)),
    };
    const { user } = renderApp(client);

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server.');
    failLoad = false;
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await within(await messageLog()).findByText('You spent 1,200.')).toBeInTheDocument();

    await user.type(messageInput(), 'Hello{Enter}');

    expect(await screen.findByRole('alert')).toHaveTextContent('Message is too long.');
    expect(messageInput()).toHaveValue('Hello');
  });
});

const at = (second: number) => `2026-11-04T10:00:0${second}Z`;
const wire = { chatId: 'c1', userId: 'u1' };

// Raw E3 items as the backend sends them, including one kind the UI does not know.
const httpMessages = [
  {
    ...wire,
    messageId: 'm1',
    sender: 'user',
    kind: 'request',
    content: 'Tidy my files',
    status: 'FAILED',
    failureReason: 'The file store was unavailable',
    createdAt: at(0),
  },
  {
    ...wire,
    messageId: 'm2',
    sender: 'assistant',
    kind: 'proposed_action',
    content: 'Archive 12 old statements?',
    status: 'PROPOSED',
    createdAt: at(1),
  },
  {
    ...wire,
    messageId: 'm3',
    sender: 'user',
    kind: 'action_response',
    decision: 'approve',
    createdAt: at(2),
  },
  {
    ...wire,
    messageId: 'm4',
    sender: 'assistant',
    kind: 'telepathy',
    content: 'Unreadable',
    createdAt: at(3),
  },
];

function fakeBackend(messages: unknown[]) {
  const chats = [
    { ...wire, title: 'Cleanup', status: 'ACTIVE', createdAt: at(0), updatedAt: at(3) },
  ];
  return vi.fn(async (url: string) => {
    const items = url.includes('/messages') ? messages : chats;
    return new Response(JSON.stringify({ items, nextCursor: null }), { status: 200 });
  });
}
