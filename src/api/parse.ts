// Runtime validation of untrusted server payloads into the wire types.
import { ApiError, invalidResponse, type ApiErrorCode } from './errors';
import {
  MESSAGE_KINDS,
  PROPOSED_ACTION_STATUSES,
  REQUEST_STATUSES,
  SENDERS,
  type Chat,
  type Message,
  type MessageKind,
  type MessageStatus,
} from './types';

// Only these kinds carry a lifecycle status; each has its own set of values.
const STATUSES_BY_KIND: Partial<Record<MessageKind, readonly MessageStatus[]>> = {
  request: REQUEST_STATUSES,
  proposed_action: PROPOSED_ACTION_STATUSES,
};

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

function readOptionalString(record: JsonRecord, key: string): string | undefined {
  return isAbsent(record[key]) ? undefined : readString(record, key);
}

function readStatus(record: JsonRecord, kind: MessageKind): MessageStatus | undefined {
  const allowed = STATUSES_BY_KIND[kind];
  return allowed && !isAbsent(record.status) ? readEnum(record, 'status', allowed) : undefined;
}

/** The artifact ids in a report's `report.deliverables`; undefined when there are none. */
function readDeliverables(record: JsonRecord): string[] | undefined {
  if (isAbsent(record.report)) return undefined;
  const refs = asRecord(record.report).deliverables;
  if (isAbsent(refs)) return undefined;
  if (!Array.isArray(refs)) throw invalidResponse();
  const ids = refs.map((ref) => readString(asRecord(ref), 'artifactId'));
  return ids.length > 0 ? ids : undefined;
}

export function parseChat(value: unknown): Chat {
  const record = asRecord(value);
  return {
    chatId: readString(record, 'chatId'),
    title: readOptionalString(record, 'title') ?? '',
    updatedAt: readString(record, 'updatedAt'),
  };
}

export function parseMessage(value: unknown): Message {
  const record = asRecord(value);
  const kind = readEnum(record, 'kind', MESSAGE_KINDS);
  return {
    messageId: readString(record, 'messageId'),
    chatId: readString(record, 'chatId'),
    sender: readEnum(record, 'sender', SENDERS),
    kind,
    // An action_response may carry only a decision; its note is optional.
    content:
      kind === 'action_response'
        ? (readOptionalString(record, 'content') ?? '')
        : readString(record, 'content'),
    requestMessageId: readOptionalString(record, 'requestMessageId'),
    inReplyToMessageId: readOptionalString(record, 'inReplyToMessageId'),
    createdAt: readString(record, 'createdAt'),
    status: readStatus(record, kind),
    failureReason: readOptionalString(record, 'failureReason'),
    deliverableArtifactIds: kind === 'report' ? readDeliverables(record) : undefined,
  };
}

function readItems(value: unknown): unknown[] {
  const items = asRecord(value).items;
  if (!Array.isArray(items)) throw invalidResponse();
  return items;
}

/** List endpoints return `{ items, nextCursor }`; the UI reads the first page only. */
export function parsePageItems<T>(value: unknown, parseItem: (item: unknown) => T): T[] {
  return readItems(value).map(parseItem);
}

/**
 * Like parsePageItems, but one item the UI cannot read is skipped (and reported in the
 * console) instead of failing the whole page.
 */
export function parsePageItemsSkippingInvalid<T>(
  value: unknown,
  parseItem: (item: unknown) => T,
): T[] {
  const parsed: T[] = [];
  for (const item of readItems(value)) {
    try {
      parsed.push(parseItem(item));
    } catch (error) {
      console.warn('Skipped a list item the UI cannot display', item, error);
    }
  }
  return parsed;
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

export function parseMemory(value: unknown): import('./types').UserMemory {
  const data = asRecord(value);
  const preferences = isAbsent(data.preferences) ? null : asRecord(data.preferences);
  if (preferences && Object.values(preferences).some((value) => typeof value !== 'string'))
    throw invalidResponse();
  return {
    instructions: readOptionalString(data, 'instructions') ?? null,
    preferences: preferences as Record<string, string> | null,
  };
}
