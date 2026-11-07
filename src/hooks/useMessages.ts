import { useCallback, useEffect, useState } from 'react';
import type { ApiClient } from '../api/client';
import type { Message } from '../api/types';
import { LOADING, success, type AsyncState } from '../utils/asyncState';
import { hasActiveRequest, mergeMessages, sortMessages } from '../utils/messages';
import { useLatestRequest } from './useLatestRequest';
import { useSendRequest } from './useSendRequest';

/** Polling stands in for the SSE stream (section 11.5) until streaming is built. */
export const DEFAULT_POLL_INTERVAL_MS = 1000;

type LoadMode = 'replace' | 'merge';

/** Messages tagged with the chat they belong to, so another chat's data is never shown. */
interface OwnedMessages {
  chatId: string | null;
  state: AsyncState<Message[]>;
}

/** Messages of one chat: load on selection, poll while a request is active, and send. */
export function useMessages(client: ApiClient, chatId: string | null, pollIntervalMs: number) {
  const [owned, setOwned] = useState<OwnedMessages>({ chatId: null, state: LOADING });
  const runLatest = useLatestRequest();
  const state = owned.chatId === chatId ? owned.state : LOADING;

  const load = useCallback(
    async (mode: LoadMode) => {
      if (!chatId) return;
      if (mode === 'replace') setOwned({ chatId, state: LOADING });
      const settled = await runLatest(() => client.listMessages(chatId));
      if (settled) setOwned((prev) => ({ chatId, state: combine(prev, chatId, settled, mode) }));
    },
    [client, chatId, runLatest],
  );

  useEffect(() => {
    void load('replace');
  }, [load]);

  const shouldPoll = state.status === 'success' && hasActiveRequest(state.data);
  useEffect(() => {
    if (!shouldPoll) return;
    const timer = setTimeout(() => void load('merge'), pollIntervalMs);
    return () => clearTimeout(timer);
  }, [shouldPoll, state, load, pollIntervalMs]);

  const appendMessage = useCallback((message: Message) => {
    // Dropped when the user has switched chats while the request was in flight.
    setOwned((prev) =>
      prev.chatId === message.chatId
        ? { ...prev, state: combine(prev, message.chatId, success([message]), 'merge') }
        : prev,
    );
  }, []);

  const sender = useSendRequest(client, chatId, appendMessage);
  const reload = useCallback(() => load('replace'), [load]);
  return { state, reload, ...sender };
}

function combine(
  prev: OwnedMessages,
  chatId: string,
  settled: AsyncState<Message[]>,
  mode: LoadMode,
): AsyncState<Message[]> {
  if (settled.status !== 'success') return settled;
  const previous = mode === 'merge' && prev.chatId === chatId ? successData(prev.state) : null;
  return success(previous ? mergeMessages(previous, settled.data) : sortMessages(settled.data));
}

const successData = <T>(state: AsyncState<T>): T | null =>
  state.status === 'success' ? state.data : null;
