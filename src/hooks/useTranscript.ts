import type { ApiClient } from '../api/client';
import type { ModelCallTranscript } from '../api/traceTypes';
import { useAsyncResource } from './useAsyncResource';

const UNSUPPORTED = 'This server cannot send model call transcripts.';

/** Exactly what one model call sent and got back, loaded when the component mounts. */
export function useTranscript(
  client: ApiClient,
  chatId: string,
  requestId: string,
  modelCallId: string,
) {
  const getTranscript = client.getModelCallTranscript?.bind(client);
  return useAsyncResource<ModelCallTranscript>(
    `${chatId}/${requestId}/${modelCallId}`,
    getTranscript && (() => getTranscript(chatId, requestId, modelCallId)),
    UNSUPPORTED,
  );
}
