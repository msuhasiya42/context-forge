import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { createMockClient, type ApiClient } from '../api/client';
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

  it('names an untitled chat after its first message', async () => {
    const { user } = renderApp(
      createMockClient({ seed: { chats: [], messages: [] }, replyDelayMs: 5 }),
    );

    await user.click(await screen.findByRole('button', { name: 'New chat' }));
    await screen.findByText('Start the conversation');
    await user.type(messageInput(), 'Card bills paid?{Enter}');

    await waitFor(() =>
      expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Card bills paid?'),
    );
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
