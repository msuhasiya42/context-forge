import type { ChangeEvent } from 'react';
import controls from '../../styles/controls.module.css';
import styles from './Composer.module.css';

interface AttachButtonProps {
  onPick: (files: FileList | null) => void;
}

/** A labelled file input styled as a button; focus shows on the label via :focus-within. */
export function AttachButton({ onPick }: AttachButtonProps) {
  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    onPick(event.target.files);
    event.target.value = ''; // allow picking the same file again after removing it
  };
  return (
    <label className={styles.attach}>
      Attach
      <input
        type="file"
        multiple
        accept=".pdf,.xlsx,.csv"
        className={controls.visuallyHidden}
        onChange={handleChange}
      />
    </label>
  );
}
