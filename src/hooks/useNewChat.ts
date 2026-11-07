import { useCallback, useState } from 'react';
import type { Chat } from '../api/types';

interface UseNewChatOptions {
  chatList: readonly Chat[];
  activeChat: Chat | null;
  createChat: () => Promise<Chat | null>;
  selectChat: (chatId: string) => void;
}

/** Adds or removes `chatId` from a set without mutating it. */
function withMembership(ids: ReadonlySet<string>, chatId: string, member: boolean) {
  if (ids.has(chatId) === member) return ids;
  const next = new Set(ids);
  if (member) next.add(chatId);
  else next.delete(chatId);
  return next;
}

/**
 * Keeps at most one empty chat: "New chat" reuses a chat known to have no messages (seen
 * empty after loading, or created this session) instead of creating another one.
 */
export function useNewChat({ chatList, activeChat, createChat, selectChat }: UseNewChatOptions) {
  const [emptyIds, setEmptyIds] = useState<ReadonlySet<string>>(new Set());
  const isEmpty = (chat: Chat) => emptyIds.has(chat.chatId);
  const isInNewChat = activeChat !== null && isEmpty(activeChat);
  const reusable = isInNewChat ? activeChat : (chatList.find(isEmpty) ?? null);

  const noteMessageCount = useCallback((chatId: string, count: number) => {
    setEmptyIds((prev) => withMembership(prev, chatId, count === 0));
  }, []);

  const startNewChat = useCallback(async () => {
    if (reusable) return selectChat(reusable.chatId);
    const chat = await createChat();
    if (!chat) return;
    noteMessageCount(chat.chatId, 0);
    selectChat(chat.chatId);
  }, [reusable, createChat, selectChat, noteMessageCount]);

  return { startNewChat, isInNewChat, noteMessageCount };
}
