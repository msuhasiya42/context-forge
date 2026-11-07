import { useCallback, useState } from 'react';
import type { ApiClient } from '../api/client';
import { describeError } from '../api/errors';
import type { Chat, Message } from '../api/types';
import { isUntitled } from '../utils/chats';

interface ChatListUpdates {
  applyChat: (chat: Chat) => void;
  removeChat: (chatId: string) => void;
}

/** Chat writes (E1, E15, E16) and keeping a chat's entry current after a request is sent. */
export function useChatActions(client: ApiClient, { applyChat, removeChat }: ChatListUpdates) {
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

  /** Resolves true once the server accepted the new title and the list shows it. */
  const renameChat = useCallback(
    async (chatId: string, title: string) => {
      const renamed = await runAction(() => client.renameChat(chatId, title));
      if (renamed) applyChat(renamed);
      return renamed !== null;
    },
    [client, runAction, applyChat],
  );

  const archiveChat = useCallback(
    async (chatId: string) => {
      const archived = await runAction(() => client.archiveChat(chatId).then(() => true));
      if (archived) removeChat(chatId);
      return archived === true;
    },
    [client, runAction, removeChat],
  );

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

  return { createChat, isCreating, renameChat, archiveChat, recordRequest, actionError };
}
