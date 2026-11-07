import { useEffect, useRef } from 'react';

/** Keeps a scroll container pinned to its end whenever `itemCount` changes. */
export function useAutoScroll<T extends HTMLElement>(itemCount: number) {
  const ref = useRef<T>(null);
  useEffect(() => {
    const element = ref.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [itemCount]);
  return ref;
}
