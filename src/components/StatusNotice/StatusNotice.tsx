import controls from '../../styles/controls.module.css';
import styles from './StatusNotice.module.css';

export type StatusNoticeProps =
  | { kind: 'loading' | 'empty'; text: string }
  | { kind: 'error'; text: string; onRetry: () => void };

/** One visual language for loading, empty and error states. */
export function StatusNotice(props: StatusNoticeProps) {
  if (props.kind === 'error') {
    return (
      <div role="alert" className={styles.notice}>
        <p className={controls.errorBanner}>{props.text}</p>
        <button type="button" className={controls.button} onClick={props.onRetry}>
          Retry
        </button>
      </div>
    );
  }
  return (
    <p role={props.kind === 'loading' ? 'status' : undefined} className={styles.notice}>
      {props.text}
    </p>
  );
}
