import type { TraceToolCall } from '../../api/traceTypes';
import { useArtifactContent } from '../../hooks/useArtifactContent';
import { formatJson } from '../../utils/trace';
import { CodePane } from '../CodePane/CodePane';
import { Disclosure } from '../Disclosure/Disclosure';
import { StatusNotice } from '../StatusNotice/StatusNotice';
import styles from './DeepDive.module.css';
import type { TraceSource } from './traceSource';

interface ToolCallDetailsProps {
  source: TraceSource;
  toolCall: TraceToolCall;
}

/** A tool call's arguments, the start of its result, and the Artifact it saved. */
export function ToolCallDetails({ source, toolCall }: ToolCallDetailsProps) {
  return (
    <div className={styles.tool}>
      <CodePane label={`${toolCall.name} arguments`} text={formatJson(toolCall.arguments)} />
      {toolCall.resultSummary !== null && (
        <Disclosure summary="Result (start; the whole result is in the next call's input)">
          <CodePane label={`${toolCall.name} result`} text={toolCall.resultSummary} />
        </Disclosure>
      )}
      {toolCall.artifactId && source.client.getArtifactContent && (
        <Disclosure summary={`Saved artifact ${toolCall.artifactId}`}>
          <ArtifactPreview source={source} artifactId={toolCall.artifactId} />
        </Disclosure>
      )}
    </div>
  );
}

function ArtifactPreview({ source, artifactId }: { source: TraceSource; artifactId: string }) {
  const { state, retry } = useArtifactContent(source.client, source.chatId, artifactId);
  if (state.status === 'loading')
    return <StatusNotice kind="loading" text="Loading the artifact…" />;
  if (state.status === 'error')
    return <StatusNotice kind="error" text={state.message} onRetry={retry} />;
  return <CodePane label={`Artifact ${artifactId}`} text={state.data} />;
}
