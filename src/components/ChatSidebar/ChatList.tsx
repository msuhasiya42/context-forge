import type { Chat } from '../../api/types';
import { useInlineRename } from '../../hooks/useInlineRename';
import { ChatRow } from './ChatRow';
import styles from './ChatSidebar.module.css';

interface ChatListProps {
  chats: readonly Chat[];
  selectedChatId: string | null;
  onSelect: (chatId: string) => void;
  onRename: (chatId: string, title: string) => Promise<boolean>;
  onRequestDelete: (chat: Chat) => void;
}

export function ChatList({
  chats,
  selectedChatId,
  onSelect,
  onRename,
  onRequestDelete,
}: ChatListProps) {
  const rename = useInlineRename(onRename);
  return (
    <nav aria-label="Chat list" className={styles.nav}>
      <ul className={styles.list}>
        {chats.map((chat) => (
          <ChatRow
            key={chat.chatId}
            chat={chat}
            isSelected={chat.chatId === selectedChatId}
            rename={rename}
            onSelect={() => onSelect(chat.chatId)}
            onRequestDelete={() => onRequestDelete(chat)}
          />
        ))}
      </ul>
    </nav>
  );
}
