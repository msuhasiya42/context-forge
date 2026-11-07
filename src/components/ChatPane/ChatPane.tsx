import type { ReactNode } from 'react';
import styles from './ChatPane.module.css';

interface ChatPaneProps {
  title: string;
  /** Controls placed before the title, such as the sidebar toggle. */
  leading: ReactNode;
  children: ReactNode;
}

export function ChatPane({ title, leading, children }: ChatPaneProps) {
  return (
    <main className={styles.pane}>
      <header className={styles.header}>
        {leading}
        <h1 className={styles.title}>{title}</h1>
      </header>
      <div className={styles.body}>{children}</div>
    </main>
  );
}
