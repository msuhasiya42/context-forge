import { useEffect, useRef } from 'react';

const FOCUSABLE =
  'button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])';

const focusablesIn = (container: HTMLElement) =>
  Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));

/** Wraps Tab and Shift+Tab around the first and last focusable elements. */
function keepFocusInside(event: KeyboardEvent, container: HTMLElement): void {
  const items = focusablesIn(container);
  const first = items[0];
  const last = items[items.length - 1];
  if (!first || !last) return;
  if (document.activeElement !== (event.shiftKey ? first : last)) return;
  event.preventDefault();
  (event.shiftKey ? last : first).focus();
}

/**
 * Modal focus handling: focuses the first control on mount, keeps Tab inside, calls
 * `onEscape` on Escape, and gives focus back to the previous element on unmount.
 * The key listener is native so Escape stops here and never reaches the sidebar overlay.
 */
export function useFocusTrap<T extends HTMLElement>(onEscape: () => void) {
  const ref = useRef<T>(null);
  const onEscapeRef = useRef(onEscape);
  useEffect(() => {
    onEscapeRef.current = onEscape;
  }, [onEscape]);

  useEffect(() => {
    const container = ref.current;
    if (!container) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Tab') return keepFocusInside(event, container);
      if (event.key !== 'Escape') return;
      event.stopPropagation();
      onEscapeRef.current();
    };
    focusablesIn(container)[0]?.focus();
    container.addEventListener('keydown', handleKeyDown);
    return () => {
      container.removeEventListener('keydown', handleKeyDown);
      if (previous?.isConnected) previous.focus();
    };
  }, []);

  return ref;
}
