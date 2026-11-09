// Error codes from section 11.2 plus two client-side ones for transport problems.
export type ApiErrorCode =
  | 'validation'
  | 'unauthenticated'
  | 'not_found'
  | 'invalid_state'
  | 'not_pending'
  | 'payload_too_large'
  | 'upload_mismatch'
  | 'network'
  | 'invalid_response'
  | 'unknown';

export class ApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;

  constructor(code: ApiErrorCode, message: string, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

const FALLBACK_MESSAGE = 'Something went wrong. Please try again.';

/** Server messages are user-facing by contract; anything else gets a generic message. */
export function describeError(error: unknown): string {
  return error instanceof ApiError ? error.message : FALLBACK_MESSAGE;
}

export const invalidResponse = () =>
  new ApiError('invalid_response', 'The server sent an unexpected response.');
