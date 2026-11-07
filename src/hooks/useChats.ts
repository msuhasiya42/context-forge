import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ApiClient } from '../api/client';
import type { Chat } from '../api/types';
import { LOADING, success, type AsyncState } from '../utils/asyncState';
import { sortChatsByRecent, upsertChat } from '../utils/chats';
import { useLatestRequest } from './useLatestRequest';

/** The chat list (E2), plus a way to apply chats returned by other calls. */
export function useChats(client: ApiClient) {
  const [state, setState] = useState<AsyncState<Chat[]>>(LOADING);
  const runLatest = useLatestRequest();

  const reload = useCallback(async () => {
    setState(LOADING);
    const settled = await runLatest(() => client.listChats());
    if (settled) setState(settled);
  }, [client, runLatest]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const applyChat = useCallback((chat: Chat) => {
    setState((prev) => (prev.status === 'success' ? success(upsertChat(prev.data, chat)) : prev));
  }, []);

  // Order is derived on read, so every update path keeps "most recently updated first".
  const sortedState = useMemo(
    () => (state.status === 'success' ? success(sortChatsByRecent(state.data)) : state),
    [state],
  );

  return { state: sortedState, reload, applyChat };
}
