import { useCallback, useRef } from 'react';
import { describeError } from '../api/errors';
import { success, type AsyncState } from '../utils/asyncState';

/**
 * Runs async tasks and settles only the most recent one. Earlier tasks resolve to `null`,
 * so a slow response for a previous chat can never overwrite the current one.
 */
export function useLatestRequest() {
  const latestId = useRef(0);

  return useCallback(async <T>(task: () => Promise<T>): Promise<AsyncState<T> | null> => {
    const id = ++latestId.current;
    let settled: AsyncState<T>;
    try {
      settled = success(await task());
    } catch (error) {
      settled = { status: 'error', message: describeError(error) };
    }
    return id === latestId.current ? settled : null;
  }, []);
}
