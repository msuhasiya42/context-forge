// Pure views of a request trace: the tree (planner, tasks, calls, tool calls) and the list
// (every step in order), plus the number formats both use.
import type {
  RequestTrace,
  TraceEvent,
  TraceEventKind,
  TraceModelCall,
  TraceReport,
  TraceTask,
  TraceToolCall,
  TranscriptMessage,
} from '../api/traceTypes';
import type { Message } from '../api/types';
import { timestampOf } from './collections';

export type PlanStep =
  | { kind: 'planner_call'; key: string; label: string; call: TraceModelCall }
  | { kind: 'clarification'; key: string; question: Message; answer: Message | null };

export interface TaskBranch {
  key: string;
  /** "Task 1", "Task 2", ... in plan order. */
  label: string;
  task: TraceTask;
  /** The labels of the tasks this one waited for. */
  dependsOn: string[];
  calls: TraceModelCall[];
}

export interface TraceTree {
  plan: PlanStep[];
  tasks: TaskBranch[];
  report: TraceReport | null;
}

export interface TraceRow {
  key: string;
  /** 1, 2, 3, ... */
  step: number;
  /** Milliseconds from the request's creation. */
  offsetMs: number;
  kind: TraceEventKind;
  label: string;
  summary: string;
  /** Set on planner and model call rows: their prompt and output can be opened. */
  call?: TraceModelCall;
  /** Set on tool call rows. */
  toolCall?: TraceToolCall;
}

const EVENT_LABELS: Record<TraceEventKind, string> = {
  planner_call: 'Planner',
  clarification: 'Question',
  clarification_answer: 'Answer',
  task_created: 'Task planned',
  task_started: 'Task started',
  model_call: 'Model call',
  tool_call: 'Tool call',
  task_completed: 'Task done',
  task_failed: 'Task ended',
  report: 'Report',
};

/** Indexes of a trace by id, shared by the tree and the list builders. */
function indexTrace(trace: RequestTrace) {
  const tasks = new Map(trace.tasks.map((task, index) => [task.taskId, { task, index }]));
  const calls = new Map<string, TraceModelCall>();
  for (const call of [...trace.plan.calls, ...trace.tasks.flatMap((task) => task.calls)])
    calls.set(call.modelCallId, call);
  const plannerNumbers = new Map(trace.plan.calls.map((call, i) => [call.modelCallId, i + 1]));
  const messages = new Map(trace.plan.clarifications.map((m) => [m.messageId, m]));
  const taskLabel = (taskId: string | undefined) => {
    const entry = taskId === undefined ? undefined : tasks.get(taskId);
    return entry ? `Task ${entry.index + 1}` : 'Task';
  };
  return { tasks, calls, plannerNumbers, messages, taskLabel };
}

/**
 * The tree view: planner calls with each question and answer where it happened, then each task
 * with its model calls (whose tool calls hang under them), then the report.
 */
export function buildTraceTree(trace: RequestTrace): TraceTree {
  const index = indexTrace(trace);
  return {
    plan: planSteps(trace, index),
    tasks: trace.tasks.map((task) => ({
      key: task.taskId,
      label: index.taskLabel(task.taskId),
      task,
      dependsOn: task.dependsOnTaskIds.map(index.taskLabel),
      calls: task.calls,
    })),
    report: trace.report,
  };
}

function planSteps(trace: RequestTrace, index: ReturnType<typeof indexTrace>): PlanStep[] {
  const answers = new Map(
    trace.plan.clarifications
      .filter((m) => m.kind === 'clarification_response' && m.inReplyToMessageId)
      .map((m) => [m.inReplyToMessageId, m]),
  );
  const steps: PlanStep[] = [];
  for (const event of trace.events) {
    const call = event.refs.modelCallId ? index.calls.get(event.refs.modelCallId) : undefined;
    if (event.kind === 'planner_call' && call) {
      const number = index.plannerNumbers.get(call.modelCallId) ?? steps.length + 1;
      steps.push({
        kind: 'planner_call',
        key: call.modelCallId,
        label: `Planner call ${number}`,
        call,
      });
    }
    const question = event.refs.messageId ? index.messages.get(event.refs.messageId) : undefined;
    if (event.kind === 'clarification' && question) {
      const answer = answers.get(question.messageId) ?? null;
      steps.push({ kind: 'clarification', key: question.messageId, question, answer });
    }
  }
  return steps;
}

/** The list view: every event in order, numbered, with its offset and a one-line summary. */
export function buildTraceList(trace: RequestTrace): TraceRow[] {
  const index = indexTrace(trace);
  const start = timestampOf(trace.request.createdAt);
  return trace.events.map((event, position) => ({
    key: eventKey(event),
    step: position + 1,
    offsetMs: Math.max(0, timestampOf(event.at) - start),
    kind: event.kind,
    label: EVENT_LABELS[event.kind],
    ...describeEvent(event, trace, index),
  }));
}

function eventKey({ kind, refs }: TraceEvent): string {
  return [kind, refs.taskId, refs.modelCallId, refs.toolCallId, refs.messageId].join(':');
}

type EventDetail = Pick<TraceRow, 'summary' | 'call' | 'toolCall'>;

function describeEvent(
  event: TraceEvent,
  trace: RequestTrace,
  index: ReturnType<typeof indexTrace>,
): EventDetail {
  const { refs } = event;
  const task = refs.taskId ? index.tasks.get(refs.taskId)?.task : undefined;
  const taskLabel = index.taskLabel(refs.taskId);
  const call = refs.modelCallId ? index.calls.get(refs.modelCallId) : undefined;
  const message = refs.messageId ? index.messages.get(refs.messageId) : undefined;
  switch (event.kind) {
    case 'planner_call': {
      const number = call ? index.plannerNumbers.get(call.modelCallId) : undefined;
      return { summary: `Planner call ${number ?? ''} · ${callFacts(call)}`, call };
    }
    case 'clarification':
      return { summary: `Asked you: ${message?.content ?? ''}` };
    case 'clarification_answer':
      return { summary: `You answered: ${message?.content ?? ''}` };
    case 'task_created':
      return { summary: `${taskLabel} planned (${task?.kind ?? 'task'}): ${task?.goal ?? ''}` };
    case 'task_started':
      return { summary: `${taskLabel} started: ${task?.goal ?? ''}` };
    case 'model_call':
      return {
        summary: `${taskLabel} · ${call?.stage ?? 'model'} call · ${callFacts(call)}`,
        call,
      };
    case 'tool_call': {
      const toolCall = call?.toolCalls.find((tool) => tool.id === refs.toolCallId);
      return { summary: `${taskLabel} · ${describeToolCall(toolCall)}`, toolCall };
    }
    case 'task_completed':
      return {
        summary: `${taskLabel} completed${task?.resultSummary ? `: ${task.resultSummary}` : ''}`,
      };
    case 'task_failed':
      return { summary: `${taskLabel} ${describeEnd(task)}` };
    case 'report':
      return { summary: describeReport(trace.report) };
  }
}

function describeEnd(task: TraceTask | undefined): string {
  const status = (task?.status ?? 'ended').toLowerCase();
  return task?.failureReason ? `${status}: ${task.failureReason}` : status;
}

export function describeToolCall(toolCall: TraceToolCall | undefined): string {
  if (!toolCall) return 'tool call';
  const outcome = toolCall.status === 'no_result' ? 'no result recorded' : toolCall.status;
  const rows = toolCall.rowCount ? `, ${plural(toolCall.rowCount, 'row')}` : '';
  return `${toolCall.name} → ${outcome}${rows}`;
}

export function describeReport(report: TraceReport | null): string {
  if (!report) return 'No report yet';
  return `Report written: ${plural(report.claims, 'claim')}, ${plural(report.uncertainties, 'uncertainty', 'uncertainties')}`;
}

/** Tokens, cost, latency and any failure of a call, for a node header or a row summary. */
export function callFacts(call: TraceModelCall | undefined): string {
  if (!call) return 'not recorded';
  const facts = [
    call.usage
      ? `${formatCount(call.usage.inputTokens)} in / ${formatCount(call.usage.outputTokens)} out tokens`
      : 'usage not reported',
    formatCost(call.estimatedCostUsd),
    formatDuration(call.durationMs),
  ];
  if (call.outcome !== 'COMPLETED')
    facts.push(
      `${call.outcome.toLowerCase()}${call.failureReason ? `: ${call.failureReason}` : ''}`,
    );
  return facts.join(' · ');
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${formatCount(count)} ${count === 1 ? one : many}`;
}

export function formatCount(count: number): string {
  return count.toLocaleString('en-US');
}

/** An estimate, so it says so; four decimals because single calls cost fractions of a cent. */
export function formatCost(usd: number | null): string {
  return usd === null ? 'cost unknown' : `~$${usd.toFixed(4)}`;
}

export function formatDuration(ms: number | null): string {
  if (ms === null) return 'no end time';
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** "+0:03", "+12:45": minutes and seconds since the request was made. */
export function formatOffset(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  return `+${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

/** One line per message the model saw: its position, its role and what it carries. */
export function transcriptHeadlines(messages: readonly TranscriptMessage[]): string[] {
  const toolNames = new Map(messages.flatMap((m) => m.toolCalls.map((c) => [c.id, c.name])));
  return messages.map((message, index) => {
    const position = `${index + 1} · ${message.role}`;
    if (message.toolCalls.length > 0)
      return `${position} · calls ${message.toolCalls.map((c) => c.name).join(', ')}`;
    if (message.role === 'tool') {
      const tool = (message.toolCallId && toolNames.get(message.toolCallId)) ?? 'a tool';
      const rows = message.rowCount ? `, ${plural(message.rowCount, 'row')}` : '';
      return `${position} · result of ${tool} · ${message.status ?? 'ok'}${rows}`;
    }
    return `${position} · ${plural(message.content.length, 'character')}`;
  });
}

/** A message's text, followed by the tool calls it asked for as JSON. */
export function transcriptMessageText(message: TranscriptMessage): string {
  const parts = [
    message.content,
    message.toolCalls.length > 0 ? formatJson(message.toolCalls) : '',
  ];
  return parts.filter(Boolean).join('\n\n') || '(empty)';
}

export function formatJson(value: unknown): string {
  return JSON.stringify(value, null, 2) ?? 'null';
}
