import type { Message } from '../../api/types';
import { useAutoScroll } from '../../hooks/useAutoScroll';
import { MessageItem } from './MessageItem';
import styles from './MessageList.module.css';

interface MessageListProps {
  messages: readonly Message[];
  onOpenReport?: (artifactId: string) => void;
}

export function MessageList({ messages, onOpenReport }: MessageListProps) {
  const listRef = useAutoScroll<HTMLOListElement>(messages.length);
  return (
    // role="log" makes new messages announced politely to screen readers.
    <ol ref={listRef} role="log" aria-label="Messages" className={styles.list}>
      {messages.map((message) => (
        <MessageItem key={message.messageId} message={message} onOpenReport={onOpenReport} />
      ))}
    </ol>
  );
}
