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
  /** Present on `kind: "request"` only. */
  status?: RequestStatus;
}
