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
