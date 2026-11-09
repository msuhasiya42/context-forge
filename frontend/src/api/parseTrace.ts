// Runtime validation of the Deep Dive payloads (trace and transcript) into their wire types.
import { invalidResponse } from './errors';
import {
  asRecord,
  isAbsent,
  parseMessage,
  readOptionalString,
  readString,
  type JsonRecord,
} from './parse';
import {
  TRACE_EVENT_KINDS,
  type ModelCallTranscript,
  type RequestTrace,
  type TraceEvent,
  type TraceEventKind,
  type TraceModelCall,
  type TraceTask,
  type TraceToolCall,
  type TraceUsage,
  type TranscriptMessage,
  type TranscriptToolUse,
} from './traceTypes';

function readNumber(record: JsonRecord, key: string): number {
  const value = record[key];
  if (typeof value !== 'number' || !Number.isFinite(value)) throw invalidResponse();
  return value;
}

function readOptionalNumber(record: JsonRecord, key: string): number | null {
  return isAbsent(record[key]) ? null : readNumber(record, key);
}

function readNullableString(record: JsonRecord, key: string): string | null {
  return readOptionalString(record, key) ?? null;
}

function readArray(record: JsonRecord, key: string): unknown[] {
  const value = record[key];
  if (isAbsent(value)) return [];
  if (!Array.isArray(value)) throw invalidResponse();
  return value;
}

function readStrings(record: JsonRecord, key: string): string[] {
  return readArray(record, key).map((item) => {
    if (typeof item !== 'string') throw invalidResponse();
    return item;
  });
}

function parseUsage(value: unknown): TraceUsage | null {
  if (isAbsent(value)) return null;
  const record = asRecord(value);
  return {
    inputTokens: readNumber(record, 'inputTokens'),
    outputTokens: readNumber(record, 'outputTokens'),
  };
}

function parseToolCall(value: unknown): TraceToolCall {
  const record = asRecord(value);
  return {
    id: readString(record, 'id'),
    name: readString(record, 'name'),
    arguments: record.arguments ?? null,
    status: readString(record, 'status'),
    resultSummary: readNullableString(record, 'resultSummary'),
    rowCount: readOptionalNumber(record, 'rowCount'),
    artifactId: readNullableString(record, 'artifactId'),
    resultInModelCallId: readNullableString(record, 'resultInModelCallId'),
  };
}

function parseModelCall(value: unknown): TraceModelCall {
  const record = asRecord(value);
  return {
    modelCallId: readString(record, 'modelCallId'),
    taskId: readNullableString(record, 'taskId'),
    stage: readString(record, 'stage'),
    model: readString(record, 'model'),
    outcome: readString(record, 'outcome'),
    failureReason: readNullableString(record, 'failureReason'),
    usage: parseUsage(record.usage),
    estimatedCostUsd: readOptionalNumber(record, 'estimatedCostUsd'),
    stopReason: readNullableString(record, 'stopReason'),
    startedAt: readString(record, 'startedAt'),
    durationMs: readOptionalNumber(record, 'durationMs'),
    hasTranscript: record.hasTranscript === true,
    toolCalls: readArray(record, 'toolCalls').map(parseToolCall),
  };
}

function parseTask(value: unknown): TraceTask {
  const record = asRecord(value);
  const result = isAbsent(record.result) ? null : asRecord(record.result);
  return {
    taskId: readString(record, 'taskId'),
    kind: readString(record, 'kind'),
    goal: readString(record, 'goal'),
    inputArtifactIds: readStrings(record, 'inputArtifactIds'),
    dependsOnTaskIds: readStrings(record, 'dependsOnTaskIds'),
    status: readString(record, 'status'),
    attempt: readNumber(record, 'attempt'),
    failureReason: readNullableString(record, 'failureReason'),
    resultSummary: result ? readNullableString(result, 'summary') : null,
    calls: readArray(record, 'calls').map(parseModelCall),
  };
}

function parseEvent(value: unknown): TraceEvent | null {
  const record = asRecord(value);
  const kind = readString(record, 'kind');
  // A kind added by a newer server is skipped rather than failing the whole trace.
  if (!TRACE_EVENT_KINDS.includes(kind as TraceEventKind)) return null;
  const refs = asRecord(record.refs ?? {});
  return {
    at: readString(record, 'at'),
    kind: kind as TraceEventKind,
    refs: {
      modelCallId: readOptionalString(refs, 'modelCallId'),
      taskId: readOptionalString(refs, 'taskId'),
      toolCallId: readOptionalString(refs, 'toolCallId'),
      messageId: readOptionalString(refs, 'messageId'),
    },
  };
}

export function parseRequestTrace(value: unknown): RequestTrace {
  const record = asRecord(value);
  const request = asRecord(record.request);
  const usage = asRecord(request.usage);
  const plan = asRecord(record.plan);
  const report = isAbsent(record.report) ? null : asRecord(record.report);
  return {
    request: {
      messageId: readString(request, 'messageId'),
      content: readString(request, 'content'),
      status: readString(request, 'status'),
      failureReason: readNullableString(request, 'failureReason'),
      createdAt: readString(request, 'createdAt'),
      usage: {
        modelCalls: readNumber(usage, 'modelCalls'),
        estimatedCostUsd: readNumber(usage, 'estimatedCostUsd'),
        tokens: isAbsent(usage.inputTokens) ? null : parseUsage(usage),
      },
    },
    plan: {
      calls: readArray(plan, 'calls').map(parseModelCall),
      tasksCreated: readStrings(plan, 'tasksCreated'),
      clarifications: readArray(plan, 'clarifications').map(parseMessage),
    },
    tasks: readArray(record, 'tasks').map(parseTask),
    report: report && {
      messageId: readString(report, 'messageId'),
      createdAt: readString(report, 'createdAt'),
      claims: readNumber(report, 'claims'),
      uncertainties: readNumber(report, 'uncertainties'),
    },
    events: readArray(record, 'events')
      .map(parseEvent)
      .filter((event): event is TraceEvent => event !== null),
  };
}

/** Message content as text: a string as is, text blocks joined, anything else as JSON. */
function contentText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((block) => {
        if (typeof block === 'string') return block;
        const text = asRecord(block).text;
        return typeof text === 'string' ? text : JSON.stringify(block);
      })
      .join('\n');
  }
  return JSON.stringify(content ?? '');
}

function parseToolUse(value: unknown): TranscriptToolUse {
  const record = asRecord(value);
  return {
    id: readString(record, 'id'),
    name: readString(record, 'name'),
    arguments: record.arguments ?? null,
  };
}

function parseTranscriptMessage(value: unknown): TranscriptMessage {
  const record = asRecord(value);
  return {
    role: readString(record, 'role'),
    content: contentText(record.content),
    toolCalls: readArray(record, 'toolCalls').map(parseToolUse),
    toolCallId: readNullableString(record, 'toolCallId'),
    status: readNullableString(record, 'status'),
    rowCount: readOptionalNumber(record, 'rowCount'),
    artifactId: readNullableString(record, 'artifactId'),
  };
}

export function parseTranscript(value: unknown): ModelCallTranscript {
  const record = asRecord(value);
  const request = asRecord(record.request);
  const response = isAbsent(record.response) ? null : asRecord(record.response);
  return {
    request: {
      stage: readString(request, 'stage'),
      model: readString(request, 'model'),
      system: readStrings(request, 'system'),
      messages: readArray(request, 'messages').map(parseTranscriptMessage),
      tools: readArray(request, 'tools').map((tool) => ({
        name: readString(asRecord(tool), 'name'),
        definition: tool,
      })),
      outputSchema: request.outputSchema ?? null,
      maxTokens: readOptionalNumber(request, 'maxTokens'),
    },
    response: response && {
      text: readNullableString(response, 'text'),
      toolCalls: readArray(response, 'toolCalls').map(parseToolUse),
      structured: response.structured ?? null,
      stopReason: readNullableString(response, 'stopReason'),
    },
    error: readNullableString(record, 'error'),
  };
}
