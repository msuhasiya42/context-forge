/** Returns a new array with `item` replacing the element of the same id, or appended. */
export function upsertBy<T>(items: readonly T[], item: T, idOf: (value: T) => string): T[] {
  const id = idOf(item);
  const index = items.findIndex((existing) => idOf(existing) === id);
  if (index === -1) return [...items, item];
  return items.map((existing, position) => (position === index ? item : existing));
}

export function timestampOf(iso: string): number {
  return Date.parse(iso);
}
