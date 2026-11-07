import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../api/client';
import { describeError } from '../api/errors';
import { LOADING, success, type AsyncState } from '../utils/asyncState';

const UNSUPPORTED = 'This server cannot send stored reports.';

/** The text of one Artifact, loaded when the ids change; `retry` loads it again. */
export function useArtifactContent(client: ApiClient, chatId: string, artifactId: string) {
  const [state, setState] = useState<AsyncState<string>>(LOADING);
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!client.getArtifactContent) {
      setState({ status: 'error', message: UNSUPPORTED });
      return;
    }
    // A response for ids the panel no longer shows is dropped.
    let active = true;
    setState(LOADING);
    client.getArtifactContent(chatId, artifactId).then(
      (text) => active && setState(success(text)),
      (error: unknown) => active && setState({ status: 'error', message: describeError(error) }),
    );
    return () => {
      active = false;
    };
  }, [client, chatId, artifactId, attempt]);
  const retry = useCallback(() => setAttempt((count) => count + 1), []);
  return { state, retry };
}
