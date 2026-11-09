import type { TraceModelCall } from '../../api/traceTypes';
import { callFacts } from '../../utils/trace';
import { Disclosure } from '../Disclosure/Disclosure';
import styles from './DeepDive.module.css';
import type { TraceSource } from './traceSource';
import { TranscriptView } from './TranscriptView';

interface ModelCallProps {
  source: TraceSource;
  call: TraceModelCall;
}

/** The call's model, tokens, cost and latency, and its prompt and output on demand. */
export function ModelCallDetails({ source, call }: ModelCallProps) {
  return (
    <div className={styles.call}>
      <p className={styles.facts}>
        {call.model} · {callFacts(call)}
      </p>
      <TranscriptDisclosure source={source} call={call} />
    </div>
  );
}

/** "Prompt and output", opened to load the call's transcript; a note when none was kept. */
export function TranscriptDisclosure({ source, call }: ModelCallProps) {
  if (!call.hasTranscript)
    return <p className={styles.muted}>No prompt or output was recorded for this call.</p>;
  return (
    <Disclosure summary="Prompt and output">
      <TranscriptView source={source} modelCallId={call.modelCallId} />
    </Disclosure>
  );
}
