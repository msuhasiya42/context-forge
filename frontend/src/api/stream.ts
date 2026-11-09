import { parseMessage, parseTask } from './parse';
import type { StreamEvent } from './types';

/** One stream per active request; polling remains the recovery path after any disconnect. */
export function streamRequest(
  root: string,
  chatId: string,
  requestId: string,
  onEvent: (event: StreamEvent) => void,
): () => void {
  const stream = new EventSource(
    `${root}/chats/${encodeURIComponent(chatId)}/requests/${encodeURIComponent(requestId)}/events`,
  );
  const message = (event: MessageEvent<string>) => {
    try {
      const value = parseMessage(JSON.parse(event.data));
      onEvent({ kind: 'message', message: value });
      if (
        value.kind === 'request' &&
        value.status &&
        !['ACCEPTED', 'RUNNING'].includes(value.status)
      )
        stream.close();
    } catch {
      stream.close();
    }
  };
  stream.addEventListener('message', message);
  stream.addEventListener('request', message);
  stream.addEventListener('activity', (event) => {
    try {
      const data: unknown = JSON.parse((event as MessageEvent<string>).data);
      if (
        typeof data === 'object' &&
        data !== null &&
        'text' in data &&
        typeof data.text === 'string'
      )
        onEvent({ kind: 'activity', text: data.text });
    } catch {
      /* An invalid activity item must not stop persisted updates. */
    }
  });
  stream.addEventListener('task', (event) => {
    try {
      onEvent({ kind: 'task', task: parseTask(JSON.parse((event as MessageEvent<string>).data)) });
    } catch {
      /* An unreadable task update must not stop the stream; the report still arrives. */
    }
  });
  stream.onerror = () => stream.close();
  return () => stream.close();
}
