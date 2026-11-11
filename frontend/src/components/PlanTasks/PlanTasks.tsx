import type { RequestTask } from '../../api/types';
import { groupByRound, taskStatusLabel } from '../../utils/tasks';
import { Disclosure } from '../Disclosure/Disclosure';
import styles from './PlanTasks.module.css';

interface PlanTasksProps {
  /** The lead researcher's brief for the request, when it has written one. */
  brief?: string;
  tasks: readonly RequestTask[];
}

/**
 * The lead's brief (collapsed) and the tasks the request was broken into, each with its
 * status in words. Tasks are grouped by research round once the lead has started a second.
 */
export function PlanTasks({ brief, tasks }: PlanTasksProps) {
  if (tasks.length === 0 && !brief) return null;
  const rounds = groupByRound(tasks);
  return (
    <section aria-label="Plan" className={styles.plan}>
      <h2 className={styles.title}>Plan</h2>
      {brief && (
        <Disclosure summary="Research brief">
          <p className={styles.brief}>{brief}</p>
        </Disclosure>
      )}
      {rounds.length > 1 ? (
        rounds.map(({ round, tasks: roundTasks }) => (
          <div key={round} className={styles.round}>
            <h3 className={styles.roundTitle}>Round {round}</h3>
            <TaskList tasks={roundTasks} />
          </div>
        ))
      ) : (
        <TaskList tasks={tasks} />
      )}
    </section>
  );
}

function TaskList({ tasks }: { tasks: readonly RequestTask[] }) {
  return (
    <ol className={styles.list}>
      {tasks.map((task) => (
        <li key={task.taskId} className={styles.task}>
          <span className={styles.goal}>{task.goal}</span>
          <span className={styles.status}>{taskStatusLabel(task.status)}</span>
        </li>
      ))}
    </ol>
  );
}
