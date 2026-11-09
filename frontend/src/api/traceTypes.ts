// Wire types of the Deep Dive: the request trace (E17) and one model call's transcript (E18),
// backend docs/implementation-plan.md section 11.4. Only the fields this UI reads are kept.
import type { Message } from './types';

export interface TraceUsage {
  inputTokens: number;
  outputTokens: number;
}

/** One tool call a model asked for, joined to the result the next call of its task carried. */
export interface TraceToolCall {
  id: string;
  name: string;
  arguments: unknown;
  /** ok, error, ... from the tool result; `no_result` when no later call carried one. */
  status: string;
  resultSummary: string | null;
  rowCount: number | null;
  artifactId: string | null;
  /** The model call whose input carried the result. */
  resultInModelCallId: string | null;
}

export interface TraceModelCall {
  modelCallId: string;
  taskId: string | null;
  /** plan, execute, compress, ... */
  stage: string;
  /** `<provider>/<model id>`. */
  model: string;
  /** COMPLETED, ERROR, TIMEOUT or CANCELLED. */
  outcome: string;
  failureReason: string | null;
  /** Null when the provider did not report usage. */
  usage: TraceUsage | null;
  estimatedCostUsd: number | null;
  stopReason: string | null;
  startedAt: string;
  durationMs: number | null;
  hasTranscript: boolean;
  toolCalls: TraceToolCall[];
}

export interface TraceTask {
  taskId: string;
  kind: string;
  goal: string;
  inputArtifactIds: string[];
  dependsOnTaskIds: string[];
  status: string;
  attempt: number;
  failureReason: string | null;
  resultSummary: string | null;
  calls: TraceModelCall[];
}

export const TRACE_EVENT_KINDS = [
  'planner_call',
  'clarification',
  'clarification_answer',
  'task_created',
  'task_started',
  'model_call',
  'tool_call',
  'task_completed',
  'task_failed',
  'report',
] as const;
export type TraceEventKind = (typeof TRACE_EVENT_KINDS)[number];

export interface TraceEventRefs {
  modelCallId?: string;
  taskId?: string;
  toolCallId?: string;
  messageId?: string;
}

export interface TraceEvent {
  at: string;
  kind: TraceEventKind;
  refs: TraceEventRefs;
}

export interface TraceReport {
  messageId: string;
  createdAt: string;
  claims: number;
  uncertainties: number;
}

export interface RequestTrace {
  request: {
    messageId: string;
    content: string;
    status: string;
    failureReason: string | null;
    createdAt: string;
    usage: { modelCalls: number; estimatedCostUsd: number; tokens: TraceUsage | null };
  };
  plan: {
    calls: TraceModelCall[];
    tasksCreated: string[];
    /** The planner's questions and the user's answers, each answer after its question. */
    clarifications: Message[];
  };
  tasks: TraceTask[];
  report: TraceReport | null;
  /** Every step in the order it happened. */
  events: TraceEvent[];
}

export interface TranscriptToolUse {
  id: string;
  name: string;
  arguments: unknown;
}

/** One message as the model saw it. */
export interface TranscriptMessage {
  /** system, user, assistant or tool. */
  role: string;
  content: string;
  toolCalls: TranscriptToolUse[];
  toolCallId: string | null;
  status: string | null;
  rowCount: number | null;
  artifactId: string | null;
}

/** Exactly what one model call sent and got back. */
export interface ModelCallTranscript {
  request: {
    stage: string;
    model: string;
    system: string[];
    messages: TranscriptMessage[];
    tools: { name: string; definition: unknown }[];
    outputSchema: unknown;
    maxTokens: number | null;
  };
  response: {
    text: string | null;
    toolCalls: TranscriptToolUse[];
    structured: unknown;
    stopReason: string | null;
  } | null;
  error: string | null;
}
