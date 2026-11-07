import { useCallback, useState } from 'react';
import type { ApiClient } from '../api/client';
import { describeError } from '../api/errors';
import type { Chat, Message } from '../api/types';
import { isUntitled, titleFromContent } from '../utils/chats';

/** Chat writes: create (E1) and naming a chat after its first request (E15). */
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

  /** Moves the chat to the top and, on its first request, names it after the request text. */
  const recordRequest = useCallback(
    async (chat: Chat, request: Message) => {
      applyChat({ ...chat, updatedAt: request.createdAt });
      if (!isUntitled(chat)) return;
      const title = titleFromContent(request.content);
      const renamed = await runAction(() => client.renameChat(chat.chatId, title));
      if (renamed) applyChat(renamed);
    },
    [client, runAction, applyChat],
  );

  return { createChat, isCreating, recordRequest, actionError };
}
