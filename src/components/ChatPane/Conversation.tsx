import { useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { Message } from '../../api/types';
import type { AsyncState } from '../../utils/asyncState';
import { AsyncContent } from '../AsyncContent/AsyncContent';
import { Composer } from '../Composer/Composer';
import { DeepDivePanel } from '../DeepDive/DeepDivePanel';
import { MessageList } from '../MessageList/MessageList';
import { ReportPanel } from '../ReportPanel/ReportPanel';
import controls from '../../styles/controls.module.css';
import styles from './Conversation.module.css';

interface ConversationProps {
  client: ApiClient;
  chatId: string;
  messages: AsyncState<Message[]>;
  onRetry: () => void;
  onSend: (content: string) => Promise<boolean>;
  isSending: boolean;
  sendError: string | null;
  /** Set while background refreshes fail; the messages already shown stay. */
  refreshError: string | null;
}

/**
 * The selected chat's messages above its composer, with a long report or a request's Deep Dive
 * in a drawer when one is opened.
 */
export function Conversation({
  client,
  chatId,
  messages,
  onRetry,
  onSend,
  isSending,
  sendError,
  refreshError,
}: ConversationProps) {
  const [openReportId, setOpenReportId] = useState<string | null>(null);
  const [deepDiveRequestId, setDeepDiveRequestId] = useState<string | null>(null);
  const onOpenReport = client.getArtifactContent ? setOpenReportId : undefined;
  const onOpenDeepDive = client.getRequestTrace ? setDeepDiveRequestId : undefined;
  return (
    <>
      <AsyncContent
        state={messages}
        loadingText="Loading messages…"
        emptyText="Start the conversation"
        onRetry={onRetry}
      >
        {(items) => (
          <MessageList
            messages={items}
            onOpenReport={onOpenReport}
            onOpenDeepDive={onOpenDeepDive}
          />
        )}
      </AsyncContent>
      {refreshError && (
        <div className={styles.noticeSlot}>
          <p role="status" className={controls.errorBanner}>
            Could not refresh messages: {refreshError} Retrying automatically.
          </p>
        </div>
      )}
      <Composer
        onSend={onSend}
        isBlocked={isSending || messages.status !== 'success'}
        error={sendError}
      />
      {openReportId && (
        <ReportPanel
          client={client}
          chatId={chatId}
          artifactId={openReportId}
          onClose={() => setOpenReportId(null)}
        />
      )}
      {deepDiveRequestId && (
        <DeepDivePanel
          client={client}
          chatId={chatId}
          requestId={deepDiveRequestId}
          onClose={() => setDeepDiveRequestId(null)}
        />
      )}
    </>
  );
}
