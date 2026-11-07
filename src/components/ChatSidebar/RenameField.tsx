import { useEffect, useId, useRef, type KeyboardEvent } from 'react';
import type { RenameDraft } from '../../hooks/useInlineRename';
import controls from '../../styles/controls.module.css';
import styles from './ChatSidebar.module.css';

interface RenameFieldProps {
  draft: RenameDraft;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
  onBlur: () => void;
}

/** Inline name editor: Enter saves, Escape cancels, blur saves when changed. */
export function RenameField({ draft, onChange, onSubmit, onCancel, onBlur }: RenameFieldProps) {
  const inputId = useId();
  const errorId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== 'Enter' && event.key !== 'Escape') return;
    event.preventDefault();
    event.stopPropagation(); // Escape here must not also close the sidebar overlay
    if (event.key === 'Enter') onSubmit();
    else onCancel();
  };

  return (
    <div className={styles.renameField}>
      <label htmlFor={inputId} className={controls.visuallyHidden}>
        Chat name
      </label>
      <input
        ref={inputRef}
        id={inputId}
        className={styles.renameInput}
        value={draft.value}
        aria-invalid={draft.error !== null}
        aria-describedby={draft.error ? errorId : undefined}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={onBlur}
      />
      {draft.error && (
        <p id={errorId} role="alert" className={styles.renameError}>
          {draft.error}
        </p>
      )}
    </div>
  );
}
