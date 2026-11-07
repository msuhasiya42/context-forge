import type { Message } from '../../api/types';
import type { AsyncState } from '../../utils/asyncState';
import { AsyncContent } from '../AsyncContent/AsyncContent';
import { Composer } from '../Composer/Composer';
import { MessageList } from '../MessageList/MessageList';
import controls from '../../styles/controls.module.css';
import styles from './Conversation.module.css';

interface ConversationProps {
  messages: AsyncState<Message[]>;
  onRetry: () => void;
  onSend: (content: string) => Promise<boolean>;
  isSending: boolean;
  sendError: string | null;
  /** Set while background refreshes fail; the messages already shown stay. */
  refreshError: string | null;
}

/** The selected chat's messages above its composer. */
export function Conversation({
  messages,
  onRetry,
  onSend,
  isSending,
  sendError,
  refreshError,
}: ConversationProps) {
  return (
    <>
      <AsyncContent
        state={messages}
        loadingText="Loading messages…"
        emptyText="Start the conversation"
        onRetry={onRetry}
      >
        {(items) => <MessageList messages={items} />}
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
    </>
  );
}
