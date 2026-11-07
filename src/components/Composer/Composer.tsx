import { useId } from 'react';
import { useComposer } from '../../hooks/useComposer';
import controls from '../../styles/controls.module.css';
import { AttachButton } from './AttachButton';
import { AttachmentChips } from './AttachmentChips';
import styles from './Composer.module.css';

interface ComposerProps {
  onSend: (content: string) => Promise<boolean>;
  /** True while sending or while the chat cannot accept messages. */
  isBlocked: boolean;
  error: string | null;
}

export function Composer({ onSend, isBlocked, error }: ComposerProps) {
  const inputId = useId();
  const { text, setText, attachments, canSend, handleSubmit, handleKeyDown } = useComposer({
    onSend,
    isBlocked,
  });
  return (
    <form className={styles.composer} onSubmit={handleSubmit}>
      {error && (
        <p role="alert" className={controls.errorBanner}>
          {error}
        </p>
      )}
      <AttachmentChips attachments={attachments.attachments} onRemove={attachments.remove} />
      <div className={styles.row}>
        <AttachButton onPick={attachments.add} />
        <label htmlFor={inputId} className={controls.visuallyHidden}>
          Message
        </label>
        <textarea
          id={inputId}
          className={styles.input}
          rows={2}
          placeholder="Ask ContextForge…"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={handleKeyDown}
        />
        <button type="submit" className={controls.primary} disabled={!canSend}>
          Send
        </button>
      </div>
    </form>
  );
}
