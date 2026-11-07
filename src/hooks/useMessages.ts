import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../api/client';
import type { Message } from '../api/types';
import { LOADING, success, type AsyncState } from '../utils/asyncState';
import { hasActiveRequest, mergeMessages, sortMessages } from '../utils/messages';
import { useLatestRequest } from './useLatestRequest';
import { useSendRequest } from './useSendRequest';

/** Polling stands in for the SSE stream (section 11.5) until streaming is built. */
export const DEFAULT_POLL_INTERVAL_MS = 1000;
const MAX_POLL_INTERVAL_MS = 10_000;

type LoadMode = 'replace' | 'merge';

/** Messages tagged with the chat they belong to, so another chat's data is never shown. */
interface OwnedMessages {
  chatId: string | null;
  state: AsyncState<Message[]>;
  /** Consecutive failed background refreshes; drives the notice and the back-off. */
  failedRefreshes: number;
  refreshError: string | null;
}

const fresh = (chatId: string | null, state: AsyncState<Message[]>): OwnedMessages => ({
  chatId,
  state,
  failedRefreshes: 0,
  refreshError: null,
});

/** Messages of one chat: load on selection, poll while a request is active, and send. */
export function useMessages(client: ApiClient, chatId: string | null, pollIntervalMs: number) {
  const [owned, setOwned] = useState<OwnedMessages>(fresh(null, LOADING));
  const runLatest = useLatestRequest();
  const current = owned.chatId === chatId ? owned : fresh(chatId, LOADING);

  const load = useCallback(
    async (mode: LoadMode) => {
      if (!chatId) return;
      if (mode === 'replace') setOwned(fresh(chatId, LOADING));
      const settled = await runLatest(() => client.listMessages(chatId));
      if (!settled) return;
      setOwned((prev) =>
        mode === 'merge' && prev.chatId === chatId
          ? applyRefresh(prev, settled)
          : fresh(chatId, sortState(settled)),
      );
    },
    [client, chatId, runLatest],
  );

  useEffect(() => {
    void load('replace');
  }, [load]);

  const refresh = useCallback(() => void load('merge'), [load]);
  usePolling(current, refresh, pollIntervalMs);

  const appendMessage = useCallback((message: Message) => {
    // Dropped when the user has switched chats while the request was in flight.
    setOwned((prev) => (prev.chatId === message.chatId ? appendTo(prev, message) : prev));
  }, []);

  const sender = useSendRequest(client, chatId, appendMessage);
  const reload = useCallback(() => load('replace'), [load]);
  return { state: current.state, refreshError: current.refreshError, reload, ...sender };
}

/** Re-fetches while a request is active, doubling the delay after each failure (capped). */
function usePolling(owned: OwnedMessages, refresh: () => void, baseIntervalMs: number) {
  const { state, failedRefreshes } = owned;
  const shouldPoll = state.status === 'success' && hasActiveRequest(state.data);
  const delay = Math.min(baseIntervalMs * 2 ** failedRefreshes, MAX_POLL_INTERVAL_MS);
  useEffect(() => {
    if (!shouldPoll) return;
    const timer = setTimeout(refresh, delay);
    return () => clearTimeout(timer);
  }, [shouldPoll, owned, refresh, delay]);
}

/** A failed refresh keeps the messages already shown; a successful one merges by id. */
function applyRefresh(prev: OwnedMessages, settled: AsyncState<Message[]>): OwnedMessages {
  if (prev.state.status !== 'success') return fresh(prev.chatId, sortState(settled));
  if (settled.status === 'error') {
    return { ...prev, failedRefreshes: prev.failedRefreshes + 1, refreshError: settled.message };
  }
  if (settled.status !== 'success') return prev;
  return fresh(prev.chatId, success(mergeMessages(prev.state.data, settled.data)));
}

function appendTo(prev: OwnedMessages, message: Message): OwnedMessages {
  const existing = prev.state.status === 'success' ? prev.state.data : [];
  return { ...prev, state: success(mergeMessages(existing, [message])) };
}

function sortState(settled: AsyncState<Message[]>): AsyncState<Message[]> {
  return settled.status === 'success' ? success(sortMessages(settled.data)) : settled;
}
