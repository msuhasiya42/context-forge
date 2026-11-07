import { useCallback, useRef, useState } from 'react';
import type { Chat } from '../api/types';

export interface RenameDraft {
  chatId: string;
  original: string;
  value: string;
  error: string | null;
}

const EMPTY_NAME_ERROR = 'Enter a name for the chat.';

/**
 * Inline rename of one chat at a time. `save` resolves true when editing ended (saved or
 * unchanged). A ref guards against double saves from Enter followed by blur, and against
 * a blur saving after Escape cancelled.
 */
export function useInlineRename(onRename: (chatId: string, title: string) => Promise<boolean>) {
  const [draft, setDraft] = useState<RenameDraft | null>(null);
  const settling = useRef(false);

  const start = useCallback((chat: Chat) => {
    settling.current = false;
    setDraft({ chatId: chat.chatId, original: chat.title, value: chat.title, error: null });
  }, []);

  const change = useCallback((value: string) => {
    setDraft((prev) => prev && { ...prev, value, error: null });
  }, []);

  const cancel = useCallback(() => {
    settling.current = true;
    setDraft(null);
  }, []);

  const save = useCallback(async (): Promise<boolean> => {
    if (!draft || settling.current) return false;
    const title = draft.value.trim();
    if (!title) {
      setDraft({ ...draft, error: EMPTY_NAME_ERROR });
      return false;
    }
    if (title === draft.original) {
      cancel();
      return true;
    }
    settling.current = true;
    const saved = await onRename(draft.chatId, title);
    settling.current = saved;
    if (saved) setDraft(null);
    return saved;
  }, [draft, onRename, cancel]);

  const saveIfChanged = useCallback(() => {
    if (draft && draft.value !== draft.original) void save();
    else cancel();
  }, [draft, save, cancel]);

  return { draft, start, change, cancel, save, saveIfChanged };
}

export type InlineRename = ReturnType<typeof useInlineRename>;
