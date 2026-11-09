import { useTranscript } from '../../hooks/useTranscript';
import { formatJson, plural, transcriptHeadlines, transcriptMessageText } from '../../utils/trace';
import { CodePane } from '../CodePane/CodePane';
import { Disclosure } from '../Disclosure/Disclosure';
import { StatusNotice } from '../StatusNotice/StatusNotice';
import styles from './DeepDive.module.css';
import type { TraceSource } from './traceSource';

interface TranscriptViewProps {
  source: TraceSource;
  modelCallId: string;
}

/** Exactly what one model call sent (Input) and got back (Output), loaded when shown. */
export function TranscriptView({ source, modelCallId }: TranscriptViewProps) {
  const { state, retry } = useTranscript(
    source.client,
    source.chatId,
    source.requestId,
    modelCallId,
  );
  if (state.status === 'loading')
    return <StatusNotice kind="loading" text="Loading the prompt and output…" />;
  if (state.status === 'error')
    return <StatusNotice kind="error" text={state.message} onRetry={retry} />;
  const { request, response, error } = state.data;
  const headlines = transcriptHeadlines(request.messages);
  return (
    <div className={styles.transcript}>
      <section className={styles.transcriptSide} aria-label="Input">
        <h4 className={styles.sideTitle}>Input</h4>
        <p className={styles.muted}>
          {request.model}
          {request.maxTokens !== null && ` · up to ${plural(request.maxTokens, 'output token')}`}
        </p>
        {/* A transcript never changes, so a block's position is its identity. */}
        {request.system.map((block, index) => (
          <Disclosure
            key={`system-${index}`}
            summary={`System block ${index + 1} · ${plural(block.length, 'character')}`}
          >
            <CodePane label={`System block ${index + 1}`} text={block} />
          </Disclosure>
        ))}
        {request.messages.map((message, index) => (
          <Disclosure key={`message-${index}`} summary={headlines[index]}>
            <CodePane label={`Message ${index + 1}`} text={transcriptMessageText(message)} />
          </Disclosure>
        ))}
        {request.tools.length > 0 && (
          <Disclosure summary={`Tools offered: ${request.tools.map((t) => t.name).join(', ')}`}>
            <CodePane
              label="Tool definitions"
              text={formatJson(request.tools.map((t) => t.definition))}
            />
          </Disclosure>
        )}
        {request.outputSchema !== null && (
          <Disclosure summary="Output schema">
            <CodePane label="Output schema" text={formatJson(request.outputSchema)} />
          </Disclosure>
        )}
      </section>
      <section className={styles.transcriptSide} aria-label="Output">
        <h4 className={styles.sideTitle}>Output</h4>
        {error && <CodePane label="Error" text={error} />}
        {response && (
          <>
            {response.stopReason && (
              <p className={styles.muted}>Stopped: {response.stopReason.replace('_', ' ')}</p>
            )}
            {response.text && <CodePane label="Text" text={response.text} />}
            {response.toolCalls.length > 0 && (
              <CodePane label="Tool calls" text={formatJson(response.toolCalls)} />
            )}
            {response.structured !== null && (
              <CodePane label="Structured result" text={formatJson(response.structured)} />
            )}
          </>
        )}
      </section>
    </div>
  );
}
