import type { RequestTask } from '../../api/types';
import styles from './PlanTasks.module.css';

const STATUS_LABELS: Record<string, string> = {
  PLANNED: 'Waiting',
  RUNNING: 'Working',
  COMPLETED: 'Done',
  FAILED: 'Failed',
  SKIPPED: 'Skipped',
  CANCELLED: 'Cancelled',
};

/** The tasks the request was broken into, each with its status in words. */
export function PlanTasks({ tasks }: { tasks: readonly RequestTask[] }) {
  if (tasks.length === 0) return null;
  return (
    <section aria-label="Plan" className={styles.plan}>
      <h2 className={styles.title}>Plan</h2>
      <ol className={styles.list}>
        {tasks.map((task) => (
          <li key={task.taskId} className={styles.task}>
            <span className={styles.goal}>{task.goal}</span>
            <span className={styles.status}>{STATUS_LABELS[task.status] ?? task.status}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
