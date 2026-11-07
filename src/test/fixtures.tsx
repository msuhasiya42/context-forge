import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../App';
import type { ApiClient } from '../api/client';
import type { MockSeed } from '../api/mockSeed';
import type { Chat, Message } from '../api/types';

export const chat = (chatId: string, title: string, updatedAt: string): Chat => ({
  chatId,
  title,
  updatedAt,
});

export const userRequest = (
  messageId: string,
  chatId: string,
  content: string,
  createdAt: string,
): Message => ({
  messageId,
  chatId,
  sender: 'user',
  kind: 'request',
  content,
  status: 'COMPLETED',
  createdAt,
});

export const agentText = (
  messageId: string,
  chatId: string,
  content: string,
  createdAt: string,
): Message => ({
  messageId,
  chatId,
  sender: 'assistant',
  kind: 'text',
  content,
  createdAt,
});

/** Two chats: "Budget" (most recent, with a short exchange) and "Travel" (one request). */
export function twoChatSeed(): MockSeed {
  return {
    chats: [
      chat('chat-travel', 'Travel', '2026-11-01T09:00:00Z'),
      chat('chat-budget', 'Budget', '2026-11-02T09:00:00Z'),
    ],
    messages: [
      userRequest('m-1', 'chat-budget', 'How much did I spend?', '2026-11-02T08:59:00Z'),
      agentText('m-2', 'chat-budget', 'You spent 1,200.', '2026-11-02T09:00:00Z'),
      userRequest('m-3', 'chat-travel', 'Plan a trip', '2026-11-01T09:00:00Z'),
    ],
  };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

export function renderApp(client: ApiClient) {
  const user = userEvent.setup();
  render(<App client={client} pollIntervalMs={10} />);
  return { user };
}
