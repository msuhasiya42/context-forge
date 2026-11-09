import { useCallback, useState } from 'react';
import type { ApiClient } from '../api/client';
import { describeError } from '../api/errors';
import type { Message } from '../api/types';

interface SendFailure {
  chatId: string;
  message: string;
}

/** Submits a request (E4). Sending and error flags are per chat, so they never leak across a switch. */
export function useSendRequest(
  client: ApiClient,
  chatId: string | null,
  onSent: (message: Message) => void,
) {
  const [sendingChatId, setSendingChatId] = useState<string | null>(null);
  const [failure, setFailure] = useState<SendFailure | null>(null);

  const send = useCallback(
    async (content: string): Promise<Message | null> => {
      if (!chatId) return null;
      setSendingChatId(chatId);
      setFailure(null);
      try {
        const request = await client.submitRequest(chatId, content);
        onSent(request);
        return request;
      } catch (error) {
        setFailure({ chatId, message: describeError(error) });
        return null;
      } finally {
        setSendingChatId(null);
      }
    },
    [client, chatId, onSent],
  );

  return {
    send,
    isSending: chatId !== null && sendingChatId === chatId,
    sendError: failure?.chatId === chatId ? failure.message : null,
  };
}
