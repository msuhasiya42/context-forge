import type { Message } from '../../api/types';
import { classNames } from '../../utils/classNames';
import { formatMessageTime, kindLabel, senderLabel, statusLabel } from '../../utils/messages';
import styles from './MessageList.module.css';

interface MessageItemProps {
  message: Message;
}

export function MessageItem({ message }: MessageItemProps) {
  const tag = kindLabel(message.kind);
  return (
    <li
      className={classNames(
        styles.message,
        message.sender === 'user' ? styles.fromUser : styles.fromAgent,
      )}
    >
      <p className={styles.meta}>
        <span className={styles.sender}>{senderLabel(message.sender)}</span>
        {tag && <span className={styles.tag}>{tag}</span>}
        <time dateTime={message.createdAt}>{formatMessageTime(message.createdAt)}</time>
      </p>
      {message.content && <p className={styles.content}>{message.content}</p>}
      {message.status && <p className={styles.status}>Status: {statusLabel(message.status)}</p>}
      {message.failureReason && <p className={styles.status}>Reason: {message.failureReason}</p>}
    </li>
  );
}
