import type { ApiClient } from '../../api/client';
import { useArtifactContent } from '../../hooks/useArtifactContent';
import controls from '../../styles/controls.module.css';
import { downloadText } from '../../utils/download';
import { Drawer } from '../Drawer/Drawer';
import { StatusNotice } from '../StatusNotice/StatusNotice';
import { ReportMarkdown } from './ReportMarkdown';
import styles from './ReportPanel.module.css';

interface ReportPanelProps {
  client: ApiClient;
  chatId: string;
  artifactId: string;
  onClose: () => void;
}

/** A long report, opened from its chat message, in a drawer with a Download button. */
export function ReportPanel({ client, chatId, artifactId, onClose }: ReportPanelProps) {
  const { state, retry } = useArtifactContent(client, chatId, artifactId);
  const download = () => {
    if (state.status === 'success')
      downloadText(`report-${artifactId}.md`, state.data, 'text/markdown');
  };
  return (
    <Drawer
      title="Full report"
      onClose={onClose}
      actions={
        <button
          type="button"
          className={controls.button}
          onClick={download}
          disabled={state.status !== 'success'}
        >
          Download
        </button>
      }
    >
      {state.status === 'loading' && (
        <StatusNotice kind="loading" text="Loading the full report…" />
      )}
      {state.status === 'error' && (
        <StatusNotice kind="error" text={state.message} onRetry={retry} />
      )}
      {state.status === 'success' && (
        <article className={styles.report}>
          <ReportMarkdown text={state.data} />
        </article>
      )}
    </Drawer>
  );
}
