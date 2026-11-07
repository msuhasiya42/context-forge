import { useState } from 'react';
import type { ApiClient } from '../../api/client';
import type { Message } from '../../api/types';
import { describeError } from '../../api/errors';
import controls from '../../styles/controls.module.css';

export function RequestControls({
  client,
  request,
  onUpdate,
}: {
  client: ApiClient;
  request: Message | undefined;
  onUpdate: (message: Message) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (
    !request ||
    !client.cancelRequest ||
    !request.status ||
    !['ACCEPTED', 'RUNNING', 'WAITING_USER'].includes(request.status)
  )
    return null;
  const cancel = async () => {
    if (!client.cancelRequest) return;
    setBusy(true);
    setError('');
    try {
      const cancelled = await client.cancelRequest(request.chatId, request.messageId);
      onUpdate(cancelled);
    } catch (e) {
      setError(describeError(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <button className={controls.button} disabled={busy} onClick={() => void cancel()}>
        Stop research
      </button>
      {request.status === 'WAITING_USER' && (
        <p>Answer the clarification below to continue this request.</p>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
