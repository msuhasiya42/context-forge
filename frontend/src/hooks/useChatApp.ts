import { useCallback, useState } from 'react';
import { describeError } from '../api/errors';
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
  const { send, upsert, state } = messages;
  const [replyError, setReplyError] = useState<string | null>(null);
  const [replying, setReplying] = useState(false);

  const sendMessage = useCallback(
    async (content: string): Promise<boolean> => {
      if (!activeChat) return false;
      const items = state.status === 'success' ? state.data : [];
      const pending = items.find(
        (m) =>
          m.kind === 'clarification' &&
          !items.some((reply) => reply.inReplyToMessageId === m.messageId) &&
          items.some(
            (request) =>
              request.messageId === m.requestMessageId && request.status === 'WAITING_USER',
          ),
      );
      if (pending && pending.requestMessageId && client.replyToRequest) {
        setReplying(true);
        setReplyError(null);
        try {
          const reply = await client.replyToRequest(
            activeChat.chatId,
            pending.requestMessageId,
            pending.messageId,
            content,
          );
          upsert(reply);
          const root = items.find((m) => m.messageId === pending.requestMessageId);
          if (root) upsert({ ...root, status: 'RUNNING' });
          return true;
        } catch (error) {
          setReplyError(describeError(error));
          return false;
        } finally {
          setReplying(false);
        }
      }
      const request = await send(content);
      if (request) void recordRequest(activeChat, request);
      return request !== null;
    },
    [activeChat, send, recordRequest, state, client, upsert],
  );

  return {
    chats,
    actions,
    activeChat,
    selectChat: setSelectedId,
    startNewChat: newChat.startNewChat,
    isInNewChat: newChat.isInNewChat,
    messages: {
      ...messages,
      isSending: messages.isSending || replying,
      sendError: replyError ?? messages.sendError,
    },
    sendMessage,
  };
}
