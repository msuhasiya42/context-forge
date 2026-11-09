import { useId, useState, type ReactNode } from 'react';
import styles from './Disclosure.module.css';

interface DisclosureProps {
  /** The line always shown; it is the button that shows or hides the content. */
  summary: ReactNode;
  children: ReactNode;
}

/**
 * A show/hide section. The content mounts only while open, so whatever it loads (a transcript,
 * an artifact) is fetched when the user asks for it.
 */
export function Disclosure({ summary, children }: DisclosureProps) {
  const [isOpen, setIsOpen] = useState(false);
  const contentId = useId();
  return (
    <div className={styles.disclosure}>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={isOpen}
        aria-controls={contentId}
        onClick={() => setIsOpen((open) => !open)}
      >
        {/* The arrow repeats aria-expanded for sighted users; it is not the only signal. */}
        <span aria-hidden="true" className={styles.marker}>
          {isOpen ? '▾' : '▸'}
        </span>
        <span>{summary}</span>
      </button>
      <div id={contentId} hidden={!isOpen} className={styles.content}>
        {isOpen && children}
      </div>
    </div>
  );
}
