import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * The narrow-screen chat list overlay: open state, Escape to close, focus moved into the
 * overlay on open and returned to the "Show chats" button on close.
 */
export function useSidebarOverlay() {
  const [isOpen, setIsOpen] = useState(false);
  const showButtonRef = useRef<HTMLButtonElement>(null);
  const hideButtonRef = useRef<HTMLButtonElement>(null);
  const wasOpen = useRef(false);

  useEffect(() => {
    if (isOpen) hideButtonRef.current?.focus();
    else if (wasOpen.current) showButtonRef.current?.focus();
    wasOpen.current = isOpen;
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setIsOpen(false);
    };
    document.addEventListener('keydown', closeOnEscape);
    return () => document.removeEventListener('keydown', closeOnEscape);
  }, [isOpen]);

  const open = useCallback(() => setIsOpen(true), []);
  const close = useCallback(() => setIsOpen(false), []);
  return { isOpen, open, close, showButtonRef, hideButtonRef };
}
