import { useId, type ReactNode } from 'react';
import { useFocusTrap } from '../../hooks/useFocusTrap';
import controls from '../../styles/controls.module.css';
import { classNames } from '../../utils/classNames';
import styles from './Drawer.module.css';

interface DrawerProps {
  title: string;
  /** Controls placed before Close in the header. */
  actions?: ReactNode;
  onClose: () => void;
  /** `wide` for content that needs room, such as code. */
  size?: 'default' | 'wide';
  children: ReactNode;
}

/**
 * A modal panel on the right, full height (full screen on a small screen). Focus stays inside;
 * Escape or Close shuts it and focus returns to the control that opened it.
 */
export function Drawer({ title, actions, onClose, size = 'default', children }: DrawerProps) {
  const titleId = useId();
  const panelRef = useFocusTrap<HTMLDivElement>(onClose);
  return (
    <div className={styles.backdrop}>
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className={classNames(styles.panel, size === 'wide' && styles.wide)}
      >
        <header className={styles.header}>
          <h2 id={titleId} className={styles.title}>
            {title}
          </h2>
          {actions}
          <button type="button" className={controls.button} onClick={onClose}>
            Close
          </button>
        </header>
        <div className={styles.body}>{children}</div>
      </div>
    </div>
  );
}
