import { useId } from 'react';
import { useFocusTrap } from '../../hooks/useFocusTrap';
import controls from '../../styles/controls.module.css';
import styles from './ConfirmDialog.module.css';

interface ConfirmDialogProps {
  title: string;
  message: string;
  confirmLabel: string;
  isBusy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** A small modal confirmation; Cancel gets initial focus so Enter never destroys by accident. */
export function ConfirmDialog(props: ConfirmDialogProps) {
  const titleId = useId();
  const messageId = useId();
  const dialogRef = useFocusTrap<HTMLDivElement>(props.onCancel);
  return (
    <div className={styles.backdrop}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
        className={styles.dialog}
      >
        <h2 id={titleId} className={styles.title}>
          {props.title}
        </h2>
        <p id={messageId} className={styles.message}>
          {props.message}
        </p>
        <div className={styles.actions}>
          <button type="button" className={controls.button} onClick={props.onCancel}>
            Cancel
          </button>
          <button
            type="button"
            className={controls.danger}
            onClick={props.onConfirm}
            disabled={props.isBusy}
          >
            {props.confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
