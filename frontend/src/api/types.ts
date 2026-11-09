// Wire types for the ContextForge API (backend docs/implementation-plan.md, section 11.4).
// Only the fields this UI reads are kept; the server sends more.

export const SENDERS = ['user', 'assistant', 'system'] as const;
export type Sender = (typeof SENDERS)[number];

export const MESSAGE_KINDS = [
  'request',
  'clarification',
  'clarification_response',
  'proposed_action',
  'action_response',
  'report',
  'text',
] as const;
export type MessageKind = (typeof MESSAGE_KINDS)[number];

export const REQUEST_STATUSES = [
  'ACCEPTED',
  'RUNNING',
  'WAITING_USER',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
] as const;
export type RequestStatus = (typeof REQUEST_STATUSES)[number];

export const PROPOSED_ACTION_STATUSES = [
  'PROPOSED',
  'APPROVED',
  'REJECTED',
  'EXPIRED',
  'EXECUTING',
  'EXECUTED',
  'FAILED',
  'UNCERTAIN',
] as const;
export type ProposedActionStatus = (typeof PROPOSED_ACTION_STATUSES)[number];

export type MessageStatus = RequestStatus | ProposedActionStatus;

export interface Chat {
  chatId: string;
  /** Empty until the first request names the chat. */
  title: string;
  updatedAt: string;
}

export interface Message {
  messageId: string;
  chatId: string;
  sender: Sender;
  kind: MessageKind;
  content: string;
  createdAt: string;
  /** Empty only on an `action_response` sent without a note. */
  /** Request statuses on `kind: "request"`, proposed-action statuses on `kind: "proposed_action"`. */
  status?: MessageStatus;
  /** User-facing reason when a request failed. */
  failureReason?: string;
  requestMessageId?: string;
  inReplyToMessageId?: string;
  /** On a report too long for the chat: the deliverable Artifacts holding the full report. */
  deliverableArtifactIds?: string[];
}

/** One task of a request's plan, as the event stream sends it. */
export interface RequestTask {
  taskId: string;
  /** web_search, sql_analysis, ... */
  kind: string;
  goal: string;
  /** PLANNED, RUNNING, COMPLETED, FAILED, SKIPPED or CANCELLED. */
  status: string;
}

export type StreamEvent =
  | { kind: 'message'; message: Message }
  | { kind: 'activity'; text: string }
  | { kind: 'task'; task: RequestTask };
export interface UserMemory {
  instructions: string | null;
  preferences: Record<string, string> | null;
}
