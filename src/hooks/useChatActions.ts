import { useCallback, useState } from 'react';
import type { ApiClient } from '../api/client';
import { describeError } from '../api/errors';
import type { Chat, Message } from '../api/types';
import { isUntitled } from '../utils/chats';

/** Chat writes (E1) and keeping a chat's entry current after a request is sent. */
export function useChatActions(client: ApiClient, applyChat: (chat: Chat) => void) {
  const [actionError, setActionError] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);

  const runAction = useCallback(async <T>(action: () => Promise<T>): Promise<T | null> => {
    setActionError(null);
    try {
      return await action();
    } catch (error) {
      setActionError(describeError(error));
      return null;
    }
  }, []);

  const createChat = useCallback(async () => {
    setIsCreating(true);
    const chat = await runAction(() => client.createChat());
    setIsCreating(false);
    if (chat) applyChat(chat);
    return chat;
  }, [client, runAction, applyChat]);

  /**
   * Moves the chat to the top. The server names a chat after its first request, so an
   * untitled chat is re-read from E2 to pick that title up; the UI never invents one.
   */
  const recordRequest = useCallback(
    async (chat: Chat, request: Message) => {
      applyChat({ ...chat, updatedAt: request.createdAt });
      if (!isUntitled(chat)) return;
      const chats = await runAction(() => client.listChats());
      const fresh = chats?.find((candidate) => candidate.chatId === chat.chatId);
      if (fresh) applyChat(fresh);
    },
    [client, runAction, applyChat],
  );

  return { createChat, isCreating, recordRequest, actionError };
}
