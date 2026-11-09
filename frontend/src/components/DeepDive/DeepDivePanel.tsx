import { useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { RequestTrace } from '../../api/traceTypes';
import { REQUEST_STATUSES, type RequestStatus } from '../../api/types';
import { useRequestTrace } from '../../hooks/useRequestTrace';
import controls from '../../styles/controls.module.css';
import { classNames } from '../../utils/classNames';
import { statusLabel } from '../../utils/messages';
import { buildTraceList, buildTraceTree, formatCost, formatCount } from '../../utils/trace';
import { Drawer } from '../Drawer/Drawer';
import { StatusNotice } from '../StatusNotice/StatusNotice';
import styles from './DeepDive.module.css';
import { TraceListView } from './TraceListView';
import type { TraceSource } from './traceSource';
import { TraceTreeView } from './TraceTreeView';

type TraceViewKind = 'list' | 'tree';

const VIEWS: { id: TraceViewKind; label: string }[] = [
  { id: 'list', label: 'List' },
  { id: 'tree', label: 'Tree' },
];
const STILL_WORKING: ReadonlySet<string> = new Set(['ACCEPTED', 'RUNNING', 'WAITING_USER']);

interface DeepDivePanelProps {
  client: ApiClient;
  chatId: string;
  requestId: string;
  onClose: () => void;
}

/**
 * How the agent worked on one request: the planner's calls and questions, the tasks it made,
 * each task's model calls and tool calls, with every prompt and output on demand. A list of
 * steps in order, or a tree under the planner and the tasks.
 */
export function DeepDivePanel({ client, chatId, requestId, onClose }: DeepDivePanelProps) {
  const { state, isLoading, retry } = useRequestTrace(client, chatId, requestId);
  const [view, setView] = useState<TraceViewKind>('list');
  const source: TraceSource = { client, chatId, requestId };
  return (
    <Drawer
      title="Deep dive"
      size="wide"
      onClose={onClose}
      actions={
        <button type="button" className={controls.button} onClick={retry} disabled={isLoading}>
          {isLoading ? 'Refreshing…' : 'Refresh'}
        </button>
      }
    >
      {state.status === 'loading' && (
        <StatusNotice kind="loading" text="Loading how this request was worked…" />
      )}
      {state.status === 'error' && (
        <StatusNotice kind="error" text={state.message} onRetry={retry} />
      )}
      {state.status === 'success' && (
        <TraceContent trace={state.data} view={view} onViewChange={setView} source={source} />
      )}
    </Drawer>
  );
}

interface TraceContentProps {
  trace: RequestTrace;
  view: TraceViewKind;
  onViewChange: (view: TraceViewKind) => void;
  source: TraceSource;
}

function TraceContent({ trace, view, onViewChange, source }: TraceContentProps) {
  const working = STILL_WORKING.has(trace.request.status);
  return (
    <div className={styles.trace}>
      <RequestSummary trace={trace} />
      {working && (
        <p role="status" className={styles.muted}>
          This request is still in progress. Refresh to see the steps since.
        </p>
      )}
      {trace.events.length === 0 ? (
        <StatusNotice kind="empty" text="Nothing has been recorded yet: the planner has not run." />
      ) : (
        <>
          <div role="group" aria-label="View" className={styles.views}>
            {VIEWS.map(({ id, label }) => (
              <button
                key={id}
                type="button"
                aria-pressed={view === id}
                className={classNames(controls.button, view === id && styles.activeView)}
                onClick={() => onViewChange(id)}
              >
                {label}
              </button>
            ))}
          </div>
          {view === 'list' ? (
            <TraceListView source={source} rows={buildTraceList(trace)} />
          ) : (
            <TraceTreeView source={source} tree={buildTraceTree(trace)} />
          )}
        </>
      )}
    </div>
  );
}

function RequestSummary({ trace }: { trace: RequestTrace }) {
  const { request } = trace;
  const { usage } = request;
  const tokens = usage.tokens
    ? `${formatCount(usage.tokens.inputTokens)} in / ${formatCount(usage.tokens.outputTokens)} out tokens`
    : 'token usage not reported';
  return (
    <section aria-label="Request" className={styles.request}>
      <p className={styles.requestText}>{request.content}</p>
      <p className={styles.muted}>
        {requestStatusLabel(request.status)} · {formatCount(usage.modelCalls)} model calls ·{' '}
        {tokens} · {formatCost(usage.estimatedCostUsd)} estimated
      </p>
      {request.failureReason && <p className={styles.result}>Reason: {request.failureReason}</p>}
    </section>
  );
}

function requestStatusLabel(status: string): string {
  return REQUEST_STATUSES.includes(status as RequestStatus)
    ? statusLabel(status as RequestStatus)
    : status;
}
