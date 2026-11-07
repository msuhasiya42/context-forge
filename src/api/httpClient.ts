import { streamRequest } from './stream';
import { parseMemory } from './parse';
import type { ApiClient } from './client';
import { ApiError, invalidResponse } from './errors';
import {
  parseApiError,
  parseChat,
  parseMessage,
  parsePageItems,
  parsePageItemsSkippingInvalid,
} from './parse';

const REQUEST_TIMEOUT_MS = 15_000;
const CHAT_PAGE_SIZE = 50;
const MESSAGE_PAGE_SIZE = 100;

type HttpMethod = 'GET' | 'POST' | 'PATCH' | 'DELETE';
type JsonCaller = (
  method: HttpMethod,
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
) => Promise<unknown>;

const chatPath = (chatId: string) => `/chats/${encodeURIComponent(chatId)}`;

/** Talks to the FastAPI backend (endpoints E1, E2, E3, E4, E15). */
export function createHttpClient(baseUrl: string): ApiClient {
  const call = createJsonCaller(baseUrl);
  const root = baseUrl.replace(/\/+$/, '');
  return {
    streamRequest: (chatId, requestId, onEvent) => streamRequest(root, chatId, requestId, onEvent),
    replyToRequest: async (chatId, requestId, targetId, content) =>
      parseMessage(
        await call(
          'POST',
          `${chatPath(chatId)}/requests/${encodeURIComponent(requestId)}/replies`,
          { inReplyToMessageId: targetId, content },
        ),
      ),
    cancelRequest: async (chatId, requestId) =>
      parseMessage(
        await call('POST', `${chatPath(chatId)}/requests/${encodeURIComponent(requestId)}/cancel`),
      ),
    getMemory: async () => parseMemory(await call('GET', '/me')),
    getArtifactContent: async (chatId, artifactId) => {
      const path = `${chatPath(chatId)}/artifacts/${encodeURIComponent(artifactId)}/content`;
      const response = await fetchWithTimeout(`${root}${path}`, { method: 'GET' });
      if (!response.ok) throw parseApiError(response.status, await readJson(response));
      return response.text();
    },
    saveMemory: async (instructions) => parseMemory(await call('PATCH', '/me', { instructions })),
    listChats: async () =>
      parsePageItems(await call('GET', `/chats?limit=${CHAT_PAGE_SIZE}`), parseChat),
    createChat: async () => parseChat(await call('POST', '/chats', {})),
    renameChat: async (chatId, title) =>
      parseChat(await call('PATCH', chatPath(chatId), { title })),
    archiveChat: async (chatId) => {
      await call('DELETE', chatPath(chatId));
    },
    listMessages: async (chatId) =>
      parsePageItemsSkippingInvalid(
        await call('GET', `${chatPath(chatId)}/messages?limit=${MESSAGE_PAGE_SIZE}`),
        parseMessage,
      ),
    submitRequest: async (chatId, content) =>
      parseMessage(
        await call(
          'POST',
          `${chatPath(chatId)}/requests`,
          { content },
          {
            // One key per submit attempt, as section 11.4 specifies.
            'Idempotency-Key': crypto.randomUUID(),
          },
        ),
      ),
  };
}

function createJsonCaller(baseUrl: string): JsonCaller {
  const root = baseUrl.replace(/\/+$/, '');
  return async (method, path, body, headers = {}) => {
    const response = await fetchWithTimeout(`${root}${path}`, {
      method,
      headers: body === undefined ? headers : { 'Content-Type': 'application/json', ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const payload = await readJson(response);
    if (!response.ok) throw parseApiError(response.status, payload);
    return payload;
  };
}

async function fetchWithTimeout(url: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(url, { ...init, signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
  } catch {
    throw new ApiError('network', 'Could not reach the server. Check your connection and retry.');
  }
}

async function readJson(response: Response): Promise<unknown> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as unknown;
  } catch {
    if (!response.ok) return null;
    throw invalidResponse();
  }
}
