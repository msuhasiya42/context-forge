import type { ApiClient } from './client';
import { ApiError } from './errors';
import { createSampleSeed, type MockSeed } from './mockSeed';
import type { Chat, Message } from './types';
import { sortChatsByRecent } from '../utils/chats';
import { sortMessages } from '../utils/messages';

export interface MockClientOptions {
  seed?: MockSeed;
  /** How long the fake agent "thinks" before replying. */
  replyDelayMs?: number;
}

const DEFAULT_REPLY_DELAY_MS = 800;
const TITLE_MAX_LENGTH = 60;

/** In-memory stand-in for the backend; the agent echoes each request after a delay. */
export function createMockClient(options: MockClientOptions = {}): ApiClient {
  const store = createMockStore(options.seed ?? createSampleSeed());
  const replyDelayMs = options.replyDelayMs ?? DEFAULT_REPLY_DELAY_MS;
  return {
    listChats: async () => sortChatsByRecent(store.chats()),
    createChat: async () =>
      store.saveChat({ chatId: store.nextId('chat'), title: '', updatedAt: now() }),
    renameChat: async (chatId, title) =>
      store.saveChat({ ...store.chat(chatId), title, updatedAt: now() }),
    listMessages: async (chatId) => sortMessages(store.messagesOf(store.chat(chatId).chatId)),
    submitRequest: async (chatId, content) => {
      const request = store.addMessage(chatId, {
        sender: 'user',
        kind: 'request',
        content,
        status: 'ACCEPTED',
      });
      nameAfterFirstRequest(store, chatId, content);
      setTimeout(() => replyTo(store, request), replyDelayMs);
      return request;
    },
  };
}

/** Mirrors the server: an untitled chat takes its first request's text, truncated. */
function nameAfterFirstRequest(store: MockStore, chatId: string, content: string): void {
  const chat = store.chat(chatId);
  if (chat.title === '') store.saveChat({ ...chat, title: titleFromContent(content) });
}

function titleFromContent(content: string): string {
  const singleLine = content.replace(/\s+/g, ' ').trim();
  if (singleLine.length <= TITLE_MAX_LENGTH) return singleLine;
  return `${singleLine.slice(0, TITLE_MAX_LENGTH - 1).trimEnd()}…`;
}

function replyTo(store: MockStore, request: Message): void {
  store.addMessage(request.chatId, {
    sender: 'assistant',
    kind: 'text',
    content: `Echo from the mock agent: ${request.content}`,
  });
  store.saveMessage({ ...request, status: 'COMPLETED' });
}

type NewMessage = Pick<Message, 'sender' | 'kind' | 'content' | 'status'>;
type MockStore = ReturnType<typeof createMockStore>;

function createMockStore(seed: MockSeed) {
  const chats = new Map(seed.chats.map((chat) => [chat.chatId, chat]));
  const messages = new Map(seed.messages.map((message) => [message.messageId, message]));
  let counter = 0;

  const store = {
    // Zero-padded so ids sort in creation order, like ULIDs.
    nextId: (prefix: string) => `${prefix}-mock-${String(++counter).padStart(6, '0')}`,
    chats: () => [...chats.values()],
    chat: (chatId: string): Chat => {
      const chat = chats.get(chatId);
      if (!chat) throw new ApiError('not_found', 'Chat not found.', 404);
      return chat;
    },
    messagesOf: (chatId: string) => [...messages.values()].filter((m) => m.chatId === chatId),
    saveChat: (chat: Chat) => (chats.set(chat.chatId, chat), chat),
    saveMessage: (message: Message) => (messages.set(message.messageId, message), message),
    addMessage: (chatId: string, fields: NewMessage): Message => {
      const chat = store.chat(chatId);
      const createdAt = now();
      store.saveChat({ ...chat, updatedAt: createdAt });
      return store.saveMessage({ ...fields, messageId: store.nextId('msg'), chatId, createdAt });
    },
  };
  return store;
}

const now = () => new Date().toISOString();
