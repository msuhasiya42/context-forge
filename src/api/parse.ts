// Runtime validation of untrusted server payloads into the wire types.
import { ApiError, invalidResponse, type ApiErrorCode } from './errors';
import { MESSAGE_KINDS, REQUEST_STATUSES, SENDERS, type Chat, type Message } from './types';

type JsonRecord = Record<string, unknown>;

function asRecord(value: unknown): JsonRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw invalidResponse();
  return value as JsonRecord;
}

function readString(record: JsonRecord, key: string): string {
  const value = record[key];
  if (typeof value !== 'string') throw invalidResponse();
  return value;
}

function readEnum<T extends string>(record: JsonRecord, key: string, allowed: readonly T[]): T {
  const value = readString(record, key);
  if (!allowed.includes(value as T)) throw invalidResponse();
  return value as T;
}

function isAbsent(value: unknown): boolean {
  return value === undefined || value === null;
}

export function parseChat(value: unknown): Chat {
  const record = asRecord(value);
  return {
    chatId: readString(record, 'chatId'),
    title: isAbsent(record.title) ? '' : readString(record, 'title'),
    updatedAt: readString(record, 'updatedAt'),
  };
}

export function parseMessage(value: unknown): Message {
  const record = asRecord(value);
  const message: Message = {
    messageId: readString(record, 'messageId'),
    chatId: readString(record, 'chatId'),
    sender: readEnum(record, 'sender', SENDERS),
    kind: readEnum(record, 'kind', MESSAGE_KINDS),
    content: readString(record, 'content'),
    createdAt: readString(record, 'createdAt'),
  };
  if (!isAbsent(record.status)) message.status = readEnum(record, 'status', REQUEST_STATUSES);
  return message;
}

/** List endpoints return `{ items, nextCursor }`; the UI reads the first page only. */
export function parsePageItems<T>(value: unknown, parseItem: (item: unknown) => T): T[] {
  const items = asRecord(value).items;
  if (!Array.isArray(items)) throw invalidResponse();
  return items.map(parseItem);
}

/** Maps the `{ error: { code, message } }` envelope; falls back when the body is not that shape. */
export function parseApiError(status: number, payload: unknown): ApiError {
  try {
    const error = asRecord(asRecord(payload).error);
    return new ApiError(
      readString(error, 'code') as ApiErrorCode,
      readString(error, 'message'),
      status,
    );
  } catch {
    return new ApiError('unknown', `Request failed with status ${status}.`, status);
  }
}
