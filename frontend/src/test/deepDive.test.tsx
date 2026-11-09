import { screen, waitFor, within } from '@testing-library/react';
import { createMockClient } from '../api/client';
import { parseRequestTrace, parseTranscript } from '../api/parseTrace';
import { buildTraceList, buildTraceTree, transcriptHeadlines } from '../utils/trace';
import { chat, renderApp, userRequest } from './fixtures';

const AT = '2026-11-07T10:00:';

function call(modelCallId: string, stage: string, startedAt: string, extra: object = {}) {
  return {
    modelCallId,
    requestMessageId: 'r1',
    taskId: null,
    stage,
    model: 'bedrock/apac.amazon.nova-lite-v1:0',
    contextVersion: 'mem:1|prompt:execute.5',
    usage: { inputTokens: 1200, outputTokens: 80 },
    estimatedCostUsd: 0.0012,
    priceVersion: '2026-11-07',
    stopReason: 'end_turn',
    toolSetHash: null,
    outcome: 'COMPLETED',
    failureReason: null,
    langsmithRunId: null,
    startedAt,
    endedAt: startedAt,
    durationMs: 1840,
    hasTranscript: true,
    toolCalls: [],
    ...extra,
  };
}

function task(taskId: string, goal: string, dependsOnTaskIds: string[], calls: object[]) {
  return {
    taskId,
    requestMessageId: 'r1',
    kind: 'sql_analysis',
    goal,
    inputArtifactIds: ['a2'],
    dependsOnTaskIds,
    status: 'COMPLETED',
    attempt: 1,
    result: { summary: `${goal} Done.` },
    failureReason: null,
    createdAt: `${AT}05Z`,
    updatedAt: `${AT}09Z`,
    calls,
  };
}

const message = (messageId: string, kind: string, sender: string, content: string, at: string) => ({
  messageId,
  chatId: 'c1',
  userId: 'u1',
  sender,
  kind,
  content,
  requestMessageId: 'r1',
  createdAt: at,
  updatedAt: at,
  ...(kind === 'clarification_response' ? { inReplyToMessageId: 'q1' } : {}),
});

const query = { dataset_id: 'a2', sql: 'SELECT * FROM t LIMIT 5' };

/** As the API sends it for a request with one question, two tasks and one tool call. */
const TRACE = {
  request: {
    messageId: 'r1',
    content: 'Compare September and October spending',
    status: 'COMPLETED',
    failureReason: null,
    createdAt: `${AT}00Z`,
    usage: { inputTokens: 6000, outputTokens: 400, modelCalls: 5, estimatedCostUsd: 0.0123 },
  },
  plan: {
    calls: [call('k1', 'plan', `${AT}01.100Z`), call('k2', 'plan', `${AT}04.000Z`)],
    tasksCreated: ['t1', 't2'],
    clarifications: [
      message('q1', 'clarification', 'assistant', 'Which months?', `${AT}02Z`),
      message('a1', 'clarification_response', 'user', 'Sep and Oct', `${AT}03Z`),
    ],
  },
  tasks: [
    task(
      't1',
      'Look at the first rows.',
      [],
      [
        call('k3', 'execute', `${AT}06.000Z`, {
          taskId: 't1',
          stopReason: 'tool_use',
          toolCalls: [
            {
              id: 'x1',
              name: 'sql_query',
              arguments: query,
              status: 'ok',
              resultSummary: 'showing 5 of 5 rows',
              rowCount: 5,
              artifactId: null,
              resultInModelCallId: 'k4',
            },
          ],
        }),
        call('k4', 'execute', `${AT}07.000Z`, { taskId: 't1' }),
      ],
    ),
    task(
      't2',
      'Total spend per month.',
      ['t1'],
      [call('k5', 'execute', `${AT}08.000Z`, { taskId: 't2', hasTranscript: false })],
    ),
  ],
  report: { messageId: 'rep', createdAt: `${AT}10Z`, claims: 4, uncertainties: 1 },
  events: [
    ['planner_call', `${AT}01.100Z`, { modelCallId: 'k1' }],
    ['clarification', `${AT}02Z`, { messageId: 'q1' }],
    ['clarification_answer', `${AT}03Z`, { messageId: 'a1' }],
    ['planner_call', `${AT}04.000Z`, { modelCallId: 'k2' }],
    ['task_created', `${AT}05Z`, { taskId: 't1' }],
    ['task_created', `${AT}05Z`, { taskId: 't2' }],
    ['task_started', `${AT}06.000Z`, { taskId: 't1' }],
    ['model_call', `${AT}06.000Z`, { modelCallId: 'k3', taskId: 't1' }],
    ['tool_call', `${AT}06.000Z`, { modelCallId: 'k3', taskId: 't1', toolCallId: 'x1' }],
    ['model_call', `${AT}07.000Z`, { modelCallId: 'k4', taskId: 't1' }],
    ['task_completed', `${AT}09Z`, { taskId: 't1' }],
    ['task_started', `${AT}08.000Z`, { taskId: 't2' }],
    ['model_call', `${AT}08.000Z`, { modelCallId: 'k5', taskId: 't2' }],
    ['task_completed', `${AT}09Z`, { taskId: 't2' }],
    ['report', `${AT}10Z`, { messageId: 'rep' }],
    ['a_kind_from_a_newer_server', `${AT}10Z`, {}],
  ].map(([kind, at, refs]) => ({ kind, at, refs })),
};

/** What call k3 sent and got back: the brief, then the model asking for sql_query. */
const TRANSCRIPT = {
  request: {
    stage: 'execute',
    model: 'bedrock/apac.amazon.nova-lite-v1:0',
    taskId: 't1',
    system: ['You are ContextForge.', 'Execute one task.'],
    messages: [
      { role: 'user', content: [{ type: 'text', text: 'Task goal: Look at the first rows.' }] },
      {
        role: 'assistant',
        content: '',
        toolCalls: [{ id: 'x0', name: 'sql_query', arguments: {} }],
      },
      { role: 'tool', content: 'showing 1 of 1 rows', toolCallId: 'x0', status: 'ok', rowCount: 1 },
    ],
    tools: [{ name: 'sql_query', description: 'Run SQL', inputSchema: { type: 'object' } }],
    outputSchema: { title: 'TaskResult' },
    maxTokens: 4000,
  },
  response: {
    text: null,
    toolCalls: [{ id: 'x1', name: 'sql_query', arguments: query }],
    structured: null,
    stopReason: 'tool_use',
    usage: { inputTokens: 1200, outputTokens: 80 },
  },
  error: null,
};

describe('trace builders', () => {
  it('builds the tree: planner calls around the question, numbered tasks with dependencies', () => {
    const tree = buildTraceTree(parseRequestTrace(TRACE));
    expect(
      tree.plan.map((step) =>
        step.kind === 'planner_call'
          ? step.label
          : `${step.question.content} -> ${step.answer?.content}`,
      ),
    ).toEqual(['Planner call 1', 'Which months? -> Sep and Oct', 'Planner call 2']);
    expect(tree.tasks.map((t) => [t.label, t.dependsOn, t.calls.length])).toEqual([
      ['Task 1', [], 2],
      ['Task 2', ['Task 1'], 1],
    ]);
    expect(tree.tasks[0]?.calls[0]?.toolCalls[0]?.resultInModelCallId).toBe('k4');
    expect(tree.report?.claims).toBe(4);
  });

  it('builds the list: every known event numbered, timed from the request, summarised', () => {
    const rows = buildTraceList(parseRequestTrace(TRACE));
    expect(rows.map((row) => row.step)).toEqual(Array.from({ length: 15 }, (_, i) => i + 1));
    expect(new Set(rows.map((row) => row.key)).size).toBe(rows.length);
    const [planner, question, answer, , created, , , modelCall, toolCall] = rows;
    expect(planner?.summary).toBe('Planner call 1 · 1,200 in / 80 out tokens · ~$0.0012 · 1.8 s');
    expect(planner?.offsetMs).toBe(1100);
    expect([question?.summary, answer?.summary]).toEqual([
      'Asked you: Which months?',
      'You answered: Sep and Oct',
    ]);
    expect(created?.summary).toBe('Task 1 planned (sql_analysis): Look at the first rows.');
    expect(modelCall?.call?.modelCallId).toBe('k3');
    expect(toolCall?.summary).toBe('Task 1 · sql_query → ok, 5 rows');
    expect(toolCall?.toolCall?.arguments).toEqual(query);
    expect(rows.at(-1)?.summary).toBe('Report written: 4 claims, 1 uncertainty');
  });

  it('names each transcript message by role and what it carries', () => {
    const { request } = parseTranscript(TRANSCRIPT);
    expect(transcriptHeadlines(request.messages)).toEqual([
      '1 · user · 34 characters',
      '2 · assistant · calls sql_query',
      '3 · tool · result of sql_query · ok, 1 row',
    ]);
  });
});

it('opens the Deep Dive from a request, switches views and shows a call’s prompt and output', async () => {
  const client = createMockClient({
    seed: {
      chats: [chat('c1', 'Spending', '2026-11-07T10:00:10Z')],
      messages: [userRequest('r1', 'c1', 'Compare September and October spending', `${AT}00Z`)],
    },
  });
  client.getRequestTrace = vi.fn(async () => parseRequestTrace(TRACE));
  client.getModelCallTranscript = vi.fn(async () => parseTranscript(TRANSCRIPT));
  const { user } = renderApp(client);

  const open = await screen.findByRole('button', { name: 'Deep dive' });
  await user.click(open);
  const panel = await screen.findByRole('dialog', { name: 'Deep dive' });
  expect(client.getRequestTrace).toHaveBeenCalledWith('c1', 'r1');
  const steps = await within(panel).findByRole('list', { name: /step by step/ });
  expect(within(steps).getAllByRole('listitem')).toHaveLength(15);
  expect(within(steps).getByText('Task 1 · sql_query → ok, 5 rows')).toBeInTheDocument();

  await user.click(within(panel).getByRole('button', { name: 'Tree' }));
  expect(within(panel).getByRole('button', { name: 'Tree' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  const tree = within(panel).getByRole('list', { name: /as a tree/ });
  expect(within(tree).getByText('Question: Which months?')).toBeInTheDocument();
  expect(within(tree).getByText('Task 2: Total spend per month.')).toBeInTheDocument();
  expect(within(tree).getByText(/after Task 1/)).toBeInTheDocument();
  expect(within(tree).getByText('No prompt or output was recorded for this call.')).toBeVisible();

  // The first task's first call: its transcript loads only when opened.
  const firstCall = within(tree).getAllByText('Call 1 · execute')[0]?.closest('li');
  if (!firstCall) throw new Error('the first task has no first call');
  expect(client.getModelCallTranscript).not.toHaveBeenCalled();
  await user.click(within(firstCall).getByRole('button', { name: /Prompt and output/ }));
  expect(client.getModelCallTranscript).toHaveBeenCalledWith('c1', 'r1', 'k3');
  const input = await within(firstCall).findByRole('region', { name: 'Input' });
  await user.click(within(input).getByRole('button', { name: /1 · user · 34 characters/ }));
  expect(within(input).getByText('Task goal: Look at the first rows.')).toBeVisible();
  const output = within(firstCall).getByRole('region', { name: 'Output' });
  expect(within(output).getByRole('button', { name: 'Copy Tool calls' })).toBeInTheDocument();
  expect(within(output).getByText(/"sql": "SELECT \* FROM t LIMIT 5"/)).toBeVisible();

  await user.keyboard('{Escape}');
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(open).toHaveFocus();
});
