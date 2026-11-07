import type { Message } from '../../api/types';
import controls from '../../styles/controls.module.css';
import { classNames } from '../../utils/classNames';
import { formatMessageTime, kindLabel, senderLabel, statusLabel } from '../../utils/messages';
import styles from './MessageList.module.css';

interface MessageItemProps {
  message: Message;
  /** Opens a report's full text; the control is shown only when this is given. */
  onOpenReport?: (artifactId: string) => void;
  /** Opens how a request was worked; shown on requests and reports when this is given. */
  onOpenDeepDive?: (requestId: string) => void;
}

export function MessageItem({ message, onOpenReport, onOpenDeepDive }: MessageItemProps) {
  const tag = kindLabel(message.kind);
  const reportId = onOpenReport && message.deliverableArtifactIds?.[0];
  const deepDiveId = onOpenDeepDive && deepDiveRequestId(message);
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
      {message.content && <p className={styles.content}>{linkedSources(message.content)}</p>}
      {(reportId || deepDiveId) && (
        <div className={styles.actions}>
          {reportId && (
            <button
              type="button"
              className={controls.button}
              onClick={() => onOpenReport?.(reportId)}
            >
              View full report
            </button>
          )}
          {deepDiveId && (
            <button
              type="button"
              className={controls.button}
              onClick={() => onOpenDeepDive?.(deepDiveId)}
            >
              Deep dive
            </button>
          )}
        </div>
      )}
      {message.status && <p className={styles.status}>Status: {statusLabel(message.status)}</p>}
      {message.failureReason && <p className={styles.status}>Reason: {message.failureReason}</p>}
    </li>
  );
}

/** The request a Deep Dive opens from this message: a request itself, or a report's request. */
function deepDiveRequestId(message: Message): string | undefined {
  if (message.kind === 'request') return message.messageId;
  return message.kind === 'report' ? message.requestMessageId : undefined;
}

function linkedSources(text: string) {
  return text.split(/(\[source\]\(https?:\/\/[^)\s]+\))/g).map((part, index) => {
    const match = /^\[source\]\((https?:\/\/[^)\s]+)\)$/.exec(part);
    return match ? (
      <a key={index} href={match[1]} target="_blank" rel="noopener noreferrer">
        source
      </a>
    ) : (
      part
    );
  });
}
