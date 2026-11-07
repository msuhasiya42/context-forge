# Web-first continuation: 7 November 2026

This is the latest implementation handoff. Read it before the earlier usage-limit handoff. Changes are in the working tree on `phase-1-engine`; nothing was merged, committed, deployed or provisioned in this continuation. The five existing milestone plan drafts were preserved.

## Scope change requested by Mayur

Build the real-model/API/memory/AWS connections around public web research first; defer uploads and extraction. Mayur explicitly selected DuckDuckGo initially; keep Google configurable. Chroma versus S3 Vectors was discussed, but no vector-store change was approved or implemented. Neither is needed for the web-first path.

## Implemented and verified offline

- Finished I-3 from the earlier final review: the code-resolved period flows through PlanResult, the brief, execute context and report coverage. I-1 and I-2 were already committed before this continuation. Fixed the three M-15 test defects (unique clarification reply IDs, checking the next execute call rather than a second compress call, truthful compaction test scope).
- Native `web_search` and `fetch_url` tools under the existing Tool Protocol. This slice does not use an MCP server. DuckDuckGo HTML search is keyless; Serper is an explicitly selected optional Google-backed provider requiring `SERPER_API_KEY`.
- `web_search` TaskKind, planner schema and v2 prompts, web execution through the existing Graph API task loop. No input upload is required.
- Search/page snapshots are private raw_result Artifacts. A task may cite its own tool-produced Artifacts as well as its authorized inputs. URL evidence must match the saved snapshot; an exact supporting excerpt is required. This validates the citation, not the truth of every natural-language inference. Claims retain UNKNOWN status. Dataset claim verification remains future work.
- URL fetches validate every redirect, reject non-public addresses and nonstandard ports, pin the validated address with original Host/TLS SNI, and cap bytes/text. Search failures return errors rather than fabricated research. No CAPTCHA bypass.
- Bedrock Converse adapter for the existing ModelClient. Defaults are APAC Nova Pro v1 for planning and Nova Lite v1 for execution/compression. Structured outputs use a synthetic result tool, not Nova v1's unsupported native output schema. Pydantic refs are expanded; planner-only calls force the result tool and executor calls retain normal tool choice.
- Model errors, bounded transient retries, per-call SDK timeouts, per-request deadline checks before model calls, and atomic in-process call admission including compression. $0.10 per attempted call is a configurable conservative budget reservation, **not a verified regional price or a hard billing guarantee**. `priceVersion` is marked `:reserve-v1`; unknown usage stays unknown, timeouts have no endedAt.
- Optional LangSmith model spans with request/task correlation and metadata-only payloads. ModelCall links the span when available. Telemetry failure cannot prevent or repeat a provider call. Full workflow/stage trace coverage is not complete.
- FastAPI composition root and queue consumer: create/list/rename/archive Chats, chronological Message paging, idempotent request submission, detail/usage, cancel, clarification replies/resume, SSE and Artifact listing. `/health`, `/me` GET/PATCH and CLI `contextforge-api` are available. External write approval/execution and upload endpoints are intentionally not implemented in this slice.
- SSE sends full request/Task/assistant Message records, bounded transient tool activity and heartbeats, and closes on terminal/waiting states. Clients refetch after disconnect; there is no replay store.
- UserRepository plus user-authored instructions/preferences, capped instructions and validated timezone. Instructions and preferences are snapshotted once per request; ModelCall records that memory version. The planner selects the last two prior reports, including their constraints. Automatic learned semantic/episodic memory, promotion, deletion and retrieval are **not** implemented.
- DynamoDB application repositories using the existing key design; conditional status transitions; transactional request idempotency and plan Task writes; S3 blobs; SQS receipt/ack and malformed-body rejection for redrive. Users also have a DynamoDB adapter. These adapters were checked with botocore Stubber, **not live AWS or a full moto conformance suite**.
- Application backend selectable as `memory` or `aws`. Both currently use the existing SQLite LangGraph saver. **The official DynamoDB graph checkpointer is still pending.** AWS mode is not a distributed recovery deployment; use one API/consumer and retain its SQLite file.
- raw_result keys now use `raw_result/<chatId>/<artifactId>.jsonl`, matching the existing bucket lifecycle prefix. Snapshots can expire after the bucket's 30-day policy while an Artifact record remains; refresh evidence rather than assuming old bytes still exist.

## Frontend delivery

The sibling `context-forge-frontend` repository was read-only under this session's filesystem permissions. Its source was not modified.

A writable copy at `/private/tmp/contextforge-frontend-preview` implements SSE with polling recovery, inline clarification replies continuing the same request, stop controls, editable standing instructions and clickable source links. The original 12 tests plus 2 new integration tests pass; TypeScript, ESLint and the production build pass. Browser visual verification was not possible.

The portable changes are saved in `patches/frontend-web-first.patch` (13 source/test files). `git apply --check` succeeds against sibling HEAD `952ec92`. Apply from that repo:

```bash
git apply --check ../context-forge/patches/frontend-web-first.patch
git apply ../context-forge/patches/frontend-web-first.patch
npm run typecheck
npm test
npm run build
VITE_API_BASE_URL=http://localhost:8000 npm run dev
```

Uploads remain unwired in the existing composer; selected file chips must not be mistaken for uploaded sources. The web-first path uses questions only. The UI is for trusted localhost and does not currently send a configured development bearer token with EventSource.

## Verification results

- Backend: **92 tests passed**; Ruff lint, format check and strict mypy passed. The suite makes no live provider or AWS calls.
- Frontend preview: **14 tests passed**, TypeScript and ESLint passed, production build passed. Existing Vite/PostCSS and Starlette/AnyIO dependency warnings remain.
- `uv sync --frozen --offline` succeeds using this machine's cached dependencies. New packages were recovered from the user's pip cache because PyPI DNS was blocked. The lockfile contains portable PyPI wheel URLs and SHA256 hashes, no temporary wheel paths.
- Real DuckDuckGo probe: ConnectError, no usable response.
- Real Bedrock Nova Lite probe: endpoint could not be reached; no successful model response was observed.
- API CLI: application startup completed, then binding `127.0.0.1:8000` was refused by this session (`operation not permitted`). Shutdown completed cleanly. Thus **no full live browser/provider acceptance has passed**.
- No LangSmith key was configured during this session, so real traces were not verified.

## Start outside the restricted agent session

```bash
uv sync --frozen
cp .env.example .env
# Check the existing contextforge AWS CLI profile; add the LangSmith key if desired.
uv run contextforge-api
```

Use the real credentials through the AWS profile; never paste access keys into source or `.env`. For AWS application storage, set `CONTEXTFORGE_BACKEND=aws` and `CONTEXTFORGE_SQS_QUEUE_URL` to the existing queue. No resources are created by startup. Credentials must authorize the configured Nova inference profiles and the application resources.

An opt-in, real-model/search smoke test uses TestClient and does not need a listening socket:

```bash
uv run python scripts/smoke_web.py --live
```

It creates a Chat, submits a public research question, polls completion and prints the report/usage. It fails acceptance on FAILED, WAITING_USER, timeout or no supported claims. It can spend money; the flag is required.

## Next work, in order

1. Run the live smoke outside this restricted session. Check DuckDuckGo response parsing and rate limits with real responses; switch provider explicitly if required. Do not call a Stubber result proof of AWS delivery.
2. Reverify account/model permissions, regional prices and LangSmith delivery; replace reservations with verified token pricing. Keep budget correctness independent of telemetry.
3. Apply the frontend patch, run browser → API → real search/model → citations, exercise clarification/cancel and test narrow-screen controls.
4. Expand DynamoDB adapter conformance coverage, integrate the official DynamoDBSaver, and test a real process restart with durable application records. Reconcile interrupted RUNNING requests; the earlier direct-runner tests use manual requeue and retained in-memory records.
5. Add the eval harness with a recorded web golden case and negative cases. The full planned evaluation/judge/replay harness remains unimplemented.
6. Implement learned memory/promotion only after verifying the standing-instruction baseline. Resolve full workflow tracing and stronger source/claim verification.
7. Streams dispatcher, real identity, cross-process budget/ownership coordination and hosting remain later deployment work. User-requested uploads/extraction, PDF retrieval, embeddings/vector stores and external writes remain deferred.

Do not mark phases 2, 4, 7 or 10 fully complete from this implementation slice. Review the new code and keep the earlier plan drafts as proposals rather than assuming all their tasks shipped.
