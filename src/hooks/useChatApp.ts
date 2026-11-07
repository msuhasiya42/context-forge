import { useCallback, useState } from 'react';
import type { ApiClient } from '../api/client';
import { pickActiveChat } from '../utils/chats';
import { useChatActions } from './useChatActions';
import { useChats } from './useChats';
import { useMessages } from './useMessages';
import { useNewChat } from './useNewChat';

/** Wires the chat list, the selection and the selected chat's messages together. */
export function useChatApp(client: ApiClient, pollIntervalMs: number) {
  const chats = useChats(client);
  const actions = useChatActions(client, chats);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const chatList = chats.state.status === 'success' ? chats.state.data : [];
  const activeChat = pickActiveChat(chatList, selectedId);
  const newChat = useNewChat({
    chatList,
    activeChat,
    createChat: actions.createChat,
    selectChat: setSelectedId,
  });
  const messages = useMessages(client, activeChat?.chatId ?? null, {
    pollIntervalMs,
    onMessageCount: newChat.noteMessageCount,
  });
  const { recordRequest } = actions;
  const { send } = messages;

  const sendMessage = useCallback(
    async (content: string): Promise<boolean> => {
      if (!activeChat) return false;
      const request = await send(content);
      if (request) void recordRequest(activeChat, request);
      return request !== null;
    },
    [activeChat, send, recordRequest],
  );

  return {
    chats,
    actions,
    activeChat,
    selectChat: setSelectedId,
    startNewChat: newChat.startNewChat,
    isInNewChat: newChat.isInNewChat,
    messages,
    sendMessage,
  };
}
