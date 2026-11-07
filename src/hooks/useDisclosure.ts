import { useCallback, useState } from 'react';

/** Open/closed state for a panel such as the narrow-screen sidebar. */
export function useDisclosure() {
  const [isOpen, setIsOpen] = useState(false);
  const open = useCallback(() => setIsOpen(true), []);
  const close = useCallback(() => setIsOpen(false), []);
  return { isOpen, open, close };
}
