export type AsyncState<T> =
  { status: 'loading' } | { status: 'error'; message: string } | { status: 'success'; data: T };

export const LOADING: AsyncState<never> = { status: 'loading' };

export const success = <T>(data: T): AsyncState<T> => ({ status: 'success', data });
