import { useState, type FormEvent, type KeyboardEvent } from 'react';
import { useAttachments } from './useAttachments';

interface UseComposerOptions {
  onSend: (content: string) => Promise<boolean>;
  isBlocked: boolean;
}

// Enter sends; Shift+Enter falls through to the textarea's newline. IME composition is respected.
const isSendKey = (event: KeyboardEvent<HTMLTextAreaElement>) =>
  event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing;

/** Draft text and picked files; cleared only after the server accepts the message. */
export function useComposer({ onSend, isBlocked }: UseComposerOptions) {
  const [text, setText] = useState('');
  const attachments = useAttachments();
  const canSend = !isBlocked && text.trim() !== '';

  const submit = async () => {
    if (!canSend) return;
    if (await onSend(text.trim())) {
      setText('');
      attachments.clear();
    }
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void submit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!isSendKey(event)) return;
    event.preventDefault();
    void submit();
  };

  return { text, setText, attachments, canSend, handleSubmit, handleKeyDown };
}
