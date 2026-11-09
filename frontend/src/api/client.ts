// The single boundary between the UI and the ContextForge API.
import { createHttpClient } from './httpClient';
import { createMockClient } from './mockClient';
import type { ModelCallTranscript, RequestTrace } from './traceTypes';
import type { Chat, Message, StreamEvent, UserMemory } from './types';

export interface ApiClient {
  streamRequest?(
    chatId: string,
    requestId: string,
    onEvent: (event: StreamEvent) => void,
  ): () => void;
  replyToRequest?(
    chatId: string,
    requestId: string,
    targetId: string,
    content: string,
  ): Promise<Message>;
  cancelRequest?(chatId: string, requestId: string): Promise<Message>;
  getMemory?(): Promise<UserMemory>;
  /** The stored text of a deliverable Artifact, such as a long report's markdown. */
  getArtifactContent?(chatId: string, artifactId: string): Promise<string>;
  saveMemory?(instructions: string): Promise<UserMemory>;
  /** E17: how a request was worked: planner calls, tasks, model and tool calls, in order. */
  getRequestTrace?(chatId: string, requestId: string): Promise<RequestTrace>;
  /** E18: exactly what one model call of the request sent and got back. */
  getModelCallTranscript?(
    chatId: string,
    requestId: string,
    modelCallId: string,
  ): Promise<ModelCallTranscript>;
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
