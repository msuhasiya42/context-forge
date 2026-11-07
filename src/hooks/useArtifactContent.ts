import type { ApiClient } from '../api/client';
import { useAsyncResource } from './useAsyncResource';

const UNSUPPORTED = 'This server cannot send stored artifacts.';

/** The text of one Artifact, loaded when the ids change; `retry` loads it again. */
export function useArtifactContent(client: ApiClient, chatId: string, artifactId: string) {
  const getContent = client.getArtifactContent?.bind(client);
  return useAsyncResource(
    `${chatId}/${artifactId}`,
    getContent && (() => getContent(chatId, artifactId)),
    UNSUPPORTED,
  );
}
