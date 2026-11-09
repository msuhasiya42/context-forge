import type { Attachment } from '../../hooks/useAttachments';
import styles from './Composer.module.css';

interface AttachmentChipsProps {
  attachments: readonly Attachment[];
  onRemove: (id: string) => void;
}

export function AttachmentChips({ attachments, onRemove }: AttachmentChipsProps) {
  if (attachments.length === 0) return null;
  return (
    <ul aria-label="Attached files" className={styles.chips}>
      {attachments.map(({ id, file }) => (
        <li key={id} className={styles.chip}>
          <span className={styles.chipName}>{file.name}</span>
          <button
            type="button"
            className={styles.chipRemove}
            aria-label={`Remove ${file.name}`}
            onClick={() => onRemove(id)}
          >
            ×
          </button>
        </li>
      ))}
    </ul>
  );
}
