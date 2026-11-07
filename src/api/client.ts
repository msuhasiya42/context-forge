// The single boundary between the UI and the ContextForge API.
import { createHttpClient } from './httpClient';
import { createMockClient } from './mockClient';
import type { Chat, Message } from './types';

export interface ApiClient {
  /** E2: my chats, most recently updated first (first page). */
  listChats(): Promise<Chat[]>;
  /** E1: create an untitled chat. */
  createChat(): Promise<Chat>;
  /** E15: rename a chat. */
  renameChat(chatId: string, title: string): Promise<Chat>;
  /** E16: archive a chat; it disappears from E2, its objects are kept. */
  archiveChat(chatId: string): Promise<void>;
  /** E3: messages in a chat (newest page). */
  listMessages(chatId: string): Promise<Message[]>;
  /** E4: submit a request; resolves with the created request Message. */
  submitRequest(chatId: string, content: string): Promise<Message>;
}

export { createHttpClient, createMockClient };

/** Uses the real backend when a base URL is configured, otherwise the in-memory mock. */
export function createApiClient(baseUrl: string | undefined): ApiClient {
  const trimmed = baseUrl?.trim();
  return trimmed ? createHttpClient(trimmed) : createMockClient();
}
