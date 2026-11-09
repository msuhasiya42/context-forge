import type { TraceModelCall } from '../../api/traceTypes';
import { taskStatusLabel } from '../../utils/tasks';
import { describeReport, describeToolCall, type TraceTree } from '../../utils/trace';
import styles from './DeepDive.module.css';
import { ModelCallDetails } from './ModelCallDetails';
import { ToolCallDetails } from './ToolCallDetails';
import type { TraceSource } from './traceSource';

interface TraceTreeViewProps {
  source: TraceSource;
  tree: TraceTree;
}

/** Planner, then each task with its model calls and their tool calls, then the report. */
export function TraceTreeView({ source, tree }: TraceTreeViewProps) {
  return (
    <ol className={styles.tree} aria-label="How the request was worked, as a tree">
      <li className={styles.branch}>
        <h3 className={styles.branchTitle}>Planner</h3>
        <ol className={styles.children}>
          {tree.plan.map((step) =>
            step.kind === 'planner_call' ? (
              <li key={step.key} className={styles.node}>
                <p className={styles.nodeTitle}>{step.label}</p>
                <ModelCallDetails source={source} call={step.call} />
              </li>
            ) : (
              <li key={step.key} className={styles.node}>
                <p className={styles.nodeTitle}>Question: {step.question.content}</p>
                <p className={styles.muted}>
                  {step.answer ? `Your answer: ${step.answer.content}` : 'Not answered yet'}
                </p>
              </li>
            ),
          )}
        </ol>
      </li>
      <li className={styles.branch}>
        <h3 className={styles.branchTitle}>Tasks ({tree.tasks.length})</h3>
        <ol className={styles.children}>
          {tree.tasks.map(({ key, label, task, dependsOn, calls }) => (
            <li key={key} className={styles.node}>
              <p className={styles.nodeTitle}>
                {label}: {task.goal}
              </p>
              <p className={styles.muted}>
                {task.kind} · {taskStatusLabel(task.status)} · attempt {task.attempt}
                {task.inputArtifactIds.length > 0 &&
                  ` · inputs ${task.inputArtifactIds.join(', ')}`}
                {dependsOn.length > 0 && ` · after ${dependsOn.join(', ')}`}
              </p>
              {task.resultSummary && <p className={styles.result}>{task.resultSummary}</p>}
              {task.failureReason && <p className={styles.result}>Reason: {task.failureReason}</p>}
              <CallList source={source} calls={calls} />
            </li>
          ))}
        </ol>
      </li>
      <li className={styles.branch}>
        <h3 className={styles.branchTitle}>Report</h3>
        <p className={styles.muted}>{describeReport(tree.report)}</p>
      </li>
    </ol>
  );
}

function CallList({ source, calls }: { source: TraceSource; calls: TraceModelCall[] }) {
  if (calls.length === 0) return <p className={styles.muted}>No model calls.</p>;
  return (
    <ol className={styles.children}>
      {calls.map((call, index) => (
        <li key={call.modelCallId} className={styles.node}>
          <p className={styles.nodeTitle}>
            Call {index + 1} · {call.stage}
          </p>
          <ModelCallDetails source={source} call={call} />
          {call.toolCalls.length > 0 && (
            <ul className={styles.children}>
              {call.toolCalls.map((toolCall) => (
                <li key={toolCall.id} className={styles.node}>
                  <p className={styles.nodeTitle}>Tool call: {describeToolCall(toolCall)}</p>
                  <ToolCallDetails source={source} toolCall={toolCall} />
                </li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ol>
  );
}
