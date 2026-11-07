import { useId } from 'react';
import type { ApiClient } from '../../api/client';
import { useArtifactContent } from '../../hooks/useArtifactContent';
import { useFocusTrap } from '../../hooks/useFocusTrap';
import controls from '../../styles/controls.module.css';
import { downloadText } from '../../utils/download';
import { StatusNotice } from '../StatusNotice/StatusNotice';
import { ReportMarkdown } from './ReportMarkdown';
import styles from './ReportPanel.module.css';

interface ReportPanelProps {
  client: ApiClient;
  chatId: string;
  artifactId: string;
  onClose: () => void;
}

/**
 * A long report, opened from its chat message, in a panel on the right (full width on a small
 * screen). Modal: focus stays inside, Escape or Close shuts it and focus returns to the button
 * that opened it.
 */
export function ReportPanel({ client, chatId, artifactId, onClose }: ReportPanelProps) {
  const titleId = useId();
  const panelRef = useFocusTrap<HTMLDivElement>(onClose);
  const { state, retry } = useArtifactContent(client, chatId, artifactId);
  const download = () => {
    if (state.status === 'success')
      downloadText(`report-${artifactId}.md`, state.data, 'text/markdown');
  };
  return (
    <div className={styles.backdrop}>
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={styles.panel}
      >
        <header className={styles.header}>
          <h2 id={titleId} className={styles.title}>
            Full report
          </h2>
          <button
            type="button"
            className={controls.button}
            onClick={download}
            disabled={state.status !== 'success'}
          >
            Download
          </button>
          <button type="button" className={controls.button} onClick={onClose}>
            Close
          </button>
        </header>
        <div className={styles.body}>
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
        </div>
      </div>
    </div>
  );
}
