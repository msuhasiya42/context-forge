import { useEffect, useRef, type ReactNode } from 'react';
import styles from './ChatPane.module.css';

interface ChatPaneProps {
  title: string;
  /** Controls placed before the title, such as the sidebar toggle. */
  leading: ReactNode;
  /** True while an overlay covers the pane; keeps focus and clicks out of it. */
  isInert: boolean;
  children: ReactNode;
}

export function ChatPane({ title, leading, isInert, children }: ChatPaneProps) {
  const mainRef = useRef<HTMLElement>(null);
  // Set as an attribute because React 18 does not know the `inert` prop.
  useEffect(() => {
    mainRef.current?.toggleAttribute('inert', isInert);
  }, [isInert]);

  return (
    <main ref={mainRef} className={styles.pane}>
      <header className={styles.header}>
        {leading}
        <h1 className={styles.title}>{title}</h1>
      </header>
      <div className={styles.body}>{children}</div>
    </main>
  );
}
