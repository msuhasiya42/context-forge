import { useCallback, useEffect, useRef, useState } from 'react';
import { describeError } from '../api/errors';
import { LOADING, success, type AsyncState } from '../utils/asyncState';

/**
 * Loads a value with `load` whenever `key` changes; `retry` loads it again. A response for a key
 * no longer shown is dropped. A reload of the same key keeps the loaded value on screen until the
 * new one arrives (`isLoading` says one is in flight). Without `load` (the client cannot fetch
 * it) the state is the error `unsupported`.
 */
export function useAsyncResource<T>(
  key: string,
  load: (() => Promise<T>) | undefined,
  unsupported: string,
) {
  const [state, setState] = useState<AsyncState<T>>(LOADING);
  const [isLoading, setIsLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);
  // The latest `load`, so a new function identity on each render does not trigger a reload.
  const loadRef = useRef(load);
  const loadedKey = useRef<string | null>(null);
  useEffect(() => {
    loadRef.current = load;
  });

  useEffect(() => {
    const current = loadRef.current;
    if (!current) {
      setState({ status: 'error', message: unsupported });
      setIsLoading(false);
      return;
    }
    let active = true;
    setIsLoading(true);
    setState((previous) =>
      previous.status === 'success' && loadedKey.current === key ? previous : LOADING,
    );
    current().then(
      (data) => {
        if (!active) return;
        loadedKey.current = key;
        setState(success(data));
        setIsLoading(false);
      },
      (error: unknown) => {
        if (!active) return;
        setState({ status: 'error', message: describeError(error) });
        setIsLoading(false);
      },
    );
    return () => {
      active = false;
    };
  }, [key, attempt, unsupported]);

  const retry = useCallback(() => setAttempt((count) => count + 1), []);
  return { state, isLoading, retry };
}
