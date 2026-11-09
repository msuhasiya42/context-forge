import { useCallback, useState } from 'react';
import type { Chat } from '../api/types';

/** Which chat awaits delete confirmation, and the archive call once confirmed. */
export function useDeleteConfirmation(onArchive: (chatId: string) => Promise<boolean>) {
  const [pending, setPending] = useState<Chat | null>(null);
  const [isDeleting, setIsDeleting] = useState(false);

  const cancel = useCallback(() => setPending(null), []);

  // Closes either way; a failure is reported by the sidebar's action error.
  const confirm = useCallback(async () => {
    if (!pending) return;
    setIsDeleting(true);
    await onArchive(pending.chatId);
    setIsDeleting(false);
    setPending(null);
  }, [pending, onArchive]);

  return { pending, isDeleting, request: setPending, cancel, confirm };
}
