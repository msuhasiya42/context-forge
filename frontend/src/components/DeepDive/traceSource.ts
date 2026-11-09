import type { ApiClient } from '../../api/client';

/** Where the Deep Dive's lazy parts (transcripts, artifacts) are loaded from. */
export interface TraceSource {
  client: ApiClient;
  chatId: string;
  requestId: string;
}
