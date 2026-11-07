import type { ReactNode } from 'react';
import type { AsyncState } from '../../utils/asyncState';
import { StatusNotice } from '../StatusNotice/StatusNotice';

interface AsyncContentProps<T> {
  state: AsyncState<readonly T[]>;
  loadingText: string;
  emptyText: string;
  onRetry: () => void;
  children: (items: readonly T[]) => ReactNode;
}

/** Renders the loading, error and empty states of a list, or the list itself. */
export function AsyncContent<T>({
  state,
  loadingText,
  emptyText,
  onRetry,
  children,
}: AsyncContentProps<T>) {
  if (state.status === 'loading') return <StatusNotice kind="loading" text={loadingText} />;
  if (state.status === 'error')
    return <StatusNotice kind="error" text={state.message} onRetry={onRetry} />;
  if (state.data.length === 0) return <StatusNotice kind="empty" text={emptyText} />;
  return <>{children(state.data)}</>;
}
