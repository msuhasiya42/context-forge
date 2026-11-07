import { useCallback, useState } from 'react';

export interface Attachment {
  id: string;
  file: File;
}

// Name, size and modification time identify a picked file well enough to skip duplicates.
const attachmentId = (file: File) => `${file.name}:${file.size}:${file.lastModified}`;

/** Files picked in the composer. Uploading is not wired yet; the names are only shown. */
export function useAttachments() {
  const [attachments, setAttachments] = useState<Attachment[]>([]);

  const add = useCallback((files: FileList | null) => {
    const picked = Array.from(files ?? [], (file) => ({ id: attachmentId(file), file }));
    setAttachments((prev) => [
      ...prev,
      ...picked.filter((item) => !prev.some((existing) => existing.id === item.id)),
    ]);
  }, []);

  const remove = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((item) => item.id !== id));
  }, []);

  const clear = useCallback(() => setAttachments([]), []);

  return { attachments, add, remove, clear };
}
