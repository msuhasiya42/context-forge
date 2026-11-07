import type { Chat, Message } from '../../api/types';
import type { AsyncState } from '../../utils/asyncState';
import { StatusNotice } from '../StatusNotice/StatusNotice';
import { Conversation } from './Conversation';

interface PaneContentProps {
  activeChat: Chat | null;
  chatsLoaded: boolean;
  messages: AsyncState<Message[]>;
  onRetryMessages: () => void;
  onSend: (content: string) => Promise<boolean>;
  isSending: boolean;
  sendError: string | null;
  refreshError: string | null;
}

/** The selected chat's conversation, or a prompt to create one once the list has loaded. */
export function PaneContent({ activeChat, chatsLoaded, ...conversation }: PaneContentProps) {
  if (activeChat) {
    // Keyed by chat so the draft and picked files reset when switching chats.
    return (
      <Conversation
        key={activeChat.chatId}
        messages={conversation.messages}
        onRetry={conversation.onRetryMessages}
        onSend={conversation.onSend}
        isSending={conversation.isSending}
        sendError={conversation.sendError}
        refreshError={conversation.refreshError}
      />
    );
  }
  return chatsLoaded ? <StatusNotice kind="empty" text="Create a chat to get started." /> : null;
}
