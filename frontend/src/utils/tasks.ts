const TASK_STATUS_LABELS: Record<string, string> = {
  PLANNED: 'Waiting',
  RUNNING: 'Working',
  COMPLETED: 'Done',
  FAILED: 'Failed',
  SKIPPED: 'Skipped',
  CANCELLED: 'Cancelled',
};

/** A Task status in words; an unknown status is shown as sent. */
export const taskStatusLabel = (status: string) => TASK_STATUS_LABELS[status] ?? status;

export interface TaskRound<T> {
  round: number;
  tasks: T[];
}

/** Tasks grouped by research round, rounds ascending, each keeping the tasks' order; a task
 * without a round belongs to round 1. */
export function groupByRound<T extends { round?: number }>(tasks: readonly T[]): TaskRound<T>[] {
  const rounds = new Map<number, T[]>();
  for (const task of tasks) {
    const round = task.round ?? 1;
    rounds.set(round, [...(rounds.get(round) ?? []), task]);
  }
  return [...rounds.entries()]
    .sort(([a], [b]) => a - b)
    .map(([round, grouped]) => ({ round, tasks: grouped }));
}
