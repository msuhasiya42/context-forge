# ContextForge frontend

Chat UI for ContextForge, an AI research agent with persistent memory. A chat list on the left and the selected chat (messages plus a composer) on the right. The backend (FastAPI plus a LangGraph worker) lives in the separate `context-forge` repository; its HTTP contract is section 11 of `docs/implementation-plan.md` there.

Stack: Vite, React 18, TypeScript (strict), CSS Modules with design tokens in `src/styles/tokens.css`, Vitest and React Testing Library.

## Run

Requires Node 20 or newer.

```bash
npm install && npm run dev
```

Open the printed URL (default http://localhost:5173). Without configuration the app uses an in-memory mock backend with two sample chats; the mock agent echoes each message after a short delay.

## Point at a backend

Copy `.env.example` to `.env.local` and set the API base URL:

```bash
VITE_API_BASE_URL=http://localhost:8000
```

When `VITE_API_BASE_URL` is unset or empty the mock client is used. The UI calls these endpoints: `POST /chats`, `GET /chats`, `GET /chats/{chatId}/messages`, `POST /chats/{chatId}/requests` (with an `Idempotency-Key` header), `PATCH /chats/{chatId}` (rename from the sidebar), `DELETE /chats/{chatId}` (archive, after an in-app confirmation) and `GET /chats/{chatId}/artifacts/{artifactId}/content` (a long report's full markdown, opened from its message with "View full report" in a right-side panel with a Download button; raw HTML in the report is dropped and unsafe link targets removed). New chats get their title from the server after the first request. The backend must allow the dev origin via CORS. `crypto.randomUUID` (used for the idempotency key) needs a secure context, so serve the UI from `localhost` or HTTPS.

## Checks

```bash
npm run lint       # ESLint (typescript-eslint, react-hooks, jsx-a11y) and Prettier --check
npm run typecheck  # tsc --noEmit
npm test           # Vitest + React Testing Library
npm run build      # typecheck and production build into dist/
npm run format     # Prettier --write
```

## Layout

- `src/api/` – the API boundary. `client.ts` exports the `ApiClient` interface, `createHttpClient`, `createMockClient` and `createApiClient` (picks one from the env). Responses are validated in `parse.ts`.
- `src/hooks/` – stateful behaviour: chat list, chat writes, messages with polling and stale-response protection, sending, composer draft and attachments.
- `src/components/` – presentation components, one folder each with its CSS Module.
- `src/utils/` – pure functions (sorting, merging, labels, titles).
- `src/test/` – the two integration-style test files and shared fixtures.

## Current limits

No streaming yet: while a request is `ACCEPTED` or `RUNNING` the open chat re-fetches its messages every second (backing off up to 10 s after failures), standing in for the SSE stream. Only the first page of chats (50) and messages (newest 100) is loaded. Picked files are shown as chips but not uploaded.
