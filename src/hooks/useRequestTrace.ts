import type { ApiClient } from '../api/client';
import type { RequestTrace } from '../api/traceTypes';
import { useAsyncResource } from './useAsyncResource';

const UNSUPPORTED = 'This server cannot show how a request was worked.';

/** The Deep Dive trace of one request; `retry` refreshes it (a running request grows). */
export function useRequestTrace(client: ApiClient, chatId: string, requestId: string) {
  const getTrace = client.getRequestTrace?.bind(client);
  return useAsyncResource<RequestTrace>(
    `${chatId}/${requestId}`,
    getTrace && (() => getTrace(chatId, requestId)),
    UNSUPPORTED,
  );
}
