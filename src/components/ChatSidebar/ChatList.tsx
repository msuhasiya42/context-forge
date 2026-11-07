import type { Chat } from '../../api/types';
import { chatDisplayName } from '../../utils/chats';
import { classNames } from '../../utils/classNames';
import styles from './ChatSidebar.module.css';

interface ChatListProps {
  chats: readonly Chat[];
  selectedChatId: string | null;
  onSelect: (chatId: string) => void;
}

export function ChatList({ chats, selectedChatId, onSelect }: ChatListProps) {
  return (
    <nav aria-label="Chat list" className={styles.nav}>
      <ul className={styles.list}>
        {chats.map((chat) => {
          const isSelected = chat.chatId === selectedChatId;
          return (
            <li key={chat.chatId}>
              <button
                type="button"
                className={classNames(styles.item, isSelected && styles.selected)}
                aria-current={isSelected ? 'true' : undefined}
                onClick={() => onSelect(chat.chatId)}
              >
                {chatDisplayName(chat)}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
