import { formatOffset, type TraceRow } from '../../utils/trace';
import styles from './DeepDive.module.css';
import { TranscriptDisclosure } from './ModelCallDetails';
import { ToolCallDetails } from './ToolCallDetails';
import type { TraceSource } from './traceSource';

interface TraceListViewProps {
  source: TraceSource;
  rows: TraceRow[];
}

/** Every step in the order it happened, with the time since the request was made. */
export function TraceListView({ source, rows }: TraceListViewProps) {
  return (
    <ol className={styles.steps} aria-label="How the request was worked, step by step">
      {rows.map((row) => (
        <li key={row.key} className={styles.step}>
          <p className={styles.stepLine}>
            <span className={styles.stepNumber}>{row.step}</span>
            <span className={styles.offset}>{formatOffset(row.offsetMs)}</span>
            <span className={styles.kind}>{row.label}</span>
            <span className={styles.summary}>{row.summary}</span>
          </p>
          {row.call && <TranscriptDisclosure source={source} call={row.call} />}
          {row.toolCall && <ToolCallDetails source={source} toolCall={row.toolCall} />}
        </li>
      ))}
    </ol>
  );
}
