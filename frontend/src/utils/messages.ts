import type { Message, MessageKind, MessageStatus, Sender } from '../api/types';
import { timestampOf, upsertBy } from './collections';

const ACTIVE_REQUEST_STATUSES: ReadonlySet<MessageStatus> = new Set(['ACCEPTED', 'RUNNING']);

const SENDER_LABELS: Record<Sender, string> = {
  user: 'You',
  assistant: 'ContextForge',
  system: 'System',
};

// Request and plain text need no tag; the other kinds are worth naming.
const KIND_LABELS: Partial<Record<MessageKind, string>> = {
  clarification: 'Question',
  clarification_response: 'Answer',
  proposed_action: 'Proposed action',
  action_response: 'Decision',
  report: 'Report',
};

const STATUS_LABELS: Record<MessageStatus, string> = {
  ACCEPTED: 'Accepted',
  RUNNING: 'Working on it',
  WAITING_USER: 'Waiting for you',
  COMPLETED: 'Completed',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
  PROPOSED: 'Awaiting your decision',
  APPROVED: 'Approved',
  REJECTED: 'Rejected',
  EXPIRED: 'Expired',
  EXECUTING: 'Executing',
  EXECUTED: 'Executed',
  UNCERTAIN: 'Outcome uncertain',
};

// The backend's pointer to a long report's deliverable: "Full report attached." after the
// counts, or "Full report attached: " before them in reports written before 7 Nov 2026.
const ATTACHED_NOTE = /\s*Full report attached[.:] ?/;

/** A report's text as the chat shows it beside its "View full report" control, which takes
 * the place of the backend's "Full report attached" note. */
export function withoutAttachedNote(content: string): string {
  return content.replace(ATTACHED_NOTE, (note) => (note.startsWith('\n') ? '\n\n' : '')).trim();
}

export const senderLabel = (sender: Sender) => SENDER_LABELS[sender];
export const kindLabel = (kind: MessageKind) => KIND_LABELS[kind] ?? null;
export const statusLabel = (status: MessageStatus) => STATUS_LABELS[status];

/** Oldest first; ULID message ids break timestamp ties in creation order. */
export function sortMessages(messages: readonly Message[]): Message[] {
  return [...messages].sort(
    (a, b) =>
      timestampOf(a.createdAt) - timestampOf(b.createdAt) || a.messageId.localeCompare(b.messageId),
  );
}

/** Upserts by id so a refresh never drops a message added locally in the meantime. */
export function mergeMessages(
  existing: readonly Message[],
  incoming: readonly Message[],
): Message[] {
  const merged = incoming.reduce(
    (all, message) => upsertBy(all, message, (value) => value.messageId),
    [...existing],
  );
  return sortMessages(merged);
}

export function hasActiveRequest(messages: readonly Message[]): boolean {
  return messages.some(
    (message) =>
      message.kind === 'request' &&
      message.status !== undefined &&
      ACTIVE_REQUEST_STATUSES.has(message.status),
  );
}

export function formatMessageTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}
