import { useState } from 'react';
import controls from '../../styles/controls.module.css';
import { classNames } from '../../utils/classNames';
import styles from './CodePane.module.css';

interface CodePaneProps {
  /** Names the pane and its copy button. */
  label: string;
  text: string;
}

/** Monospace text exactly as sent or received, with a button that copies it. */
export function CodePane({ label, text }: CodePaneProps) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard?.writeText(text).then(
      () => setCopied(true),
      () => setCopied(false),
    );
  };
  return (
    <figure className={styles.pane}>
      <figcaption className={styles.caption}>
        <span className={styles.label}>{label}</span>
        <button
          type="button"
          className={classNames(controls.button, styles.copy)}
          aria-label={`Copy ${label}`}
          onClick={copy}
        >
          {copied ? 'Copied' : 'Copy'}
        </button>
      </figcaption>
      <pre className={styles.code}>{text}</pre>
    </figure>
  );
}
