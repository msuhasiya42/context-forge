import type { Message } from '../../api/types';
import type { AsyncState } from '../../utils/asyncState';
import { AsyncContent } from '../AsyncContent/AsyncContent';
import { Composer } from '../Composer/Composer';
import { MessageList } from '../MessageList/MessageList';

interface ConversationProps {
  messages: AsyncState<Message[]>;
  onRetry: () => void;
  onSend: (content: string) => Promise<boolean>;
  isSending: boolean;
  sendError: string | null;
}

/** The selected chat's messages above its composer. */
export function Conversation({
  messages,
  onRetry,
  onSend,
  isSending,
  sendError,
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
      <Composer
        onSend={onSend}
        isBlocked={isSending || messages.status !== 'success'}
        error={sendError}
      />
    </>
  );
}
