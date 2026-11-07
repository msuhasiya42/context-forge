import type { Chat, Message } from './types';

export interface MockSeed {
  chats: Chat[];
  messages: Message[];
}

/** Sample data so the UI has something to show without a backend. */
export function createSampleSeed(): MockSeed {
  return {
    chats: [
      {
        chatId: 'chat-sample-1',
        title: 'Compare Oct vs Sep spend',
        updatedAt: '2026-11-04T10:05:00Z',
      },
      { chatId: 'chat-sample-2', title: 'Card bills paid?', updatedAt: '2026-11-03T18:00:00Z' },
    ],
    messages: [
      {
        messageId: 'msg-sample-1',
        chatId: 'chat-sample-1',
        sender: 'user',
        kind: 'request',
        content: 'Compare October spend to September, excluding transfers',
        status: 'COMPLETED',
        createdAt: '2026-11-04T10:00:00Z',
      },
      {
        messageId: 'msg-sample-2',
        chatId: 'chat-sample-1',
        sender: 'assistant',
        kind: 'text',
        content: 'October spend was 12% higher than September, mostly groceries and travel.',
        createdAt: '2026-11-04T10:05:00Z',
      },
    ],
  };
}
