# Research testing through the API

The command below connects to the running backend, creates a normal chat/request, watches its
progress and saves the result. The same user's UI can inspect that chat, its messages and Deep
Dive. Default chat titles start with `API:` so these runs are easy to recognize. Real submissions
can incur model/search charges and use the backend's existing spend and web-call limits.

## Start the backend and follow logs

After stopping the existing API with Ctrl+C, run from the backend repository root:

```sh
uv run --no-sync contextforge-api
```

This writes JSON logs to the terminal and `backend.log`. In another terminal:

```sh
tail -f backend.log
```

Each application log includes its timestamp, level and request/task IDs when in that context.
The file rotates at 10 MB with three backups. Set `CONTEXTFORGE_BACKEND_LOG_PATH` to change
the location. Known configured API keys/tokens are redacted; prompts and source contents are
not deliberately copied into ordinary logs. Detailed model transcripts remain in Deep Dive.
The default log files and `.data/` run captures are ignored by Git.

The API health response must show `build: web-research-v3`. The `run` command checks that before
creating a chat or submitting work, preventing another paid test on stale code. Restarting
the backend loads changes; refreshing the frontend does not. A frontend restart is unnecessary
for these backend fixes.

## Submit and watch one research question

```sh
uv run --no-sync python -m contextforge.evals.api_research run \
  "Find the official release pages for Python 3.12.0 and Python 3.13.0. Give each release date with a source link, then assess which is newer based on those dates. Keep the answer brief."
```

The command prints chat/request IDs, task state changes, cumulative admitted web calls, model
call counts and estimated spend. It reads the optional development token from the same local
settings as the backend. For a different backend, place `--base-url URL` before `run` or `watch`.

Default output is `.data/runs/<unique-id>/`:

| File | Contents |
|---|---|
| `run.json` | Chat/request IDs, submission idempotency key and observed backend build |
| `progress.jsonl` | Progress snapshots as status, tasks or usage change |
| `detail.json` | Latest request, task results and usage |
| `trace.json` | Final available Deep Dive trace, including provider failures |
| `bundle.json` | Report, referenced source snapshots, deliverables and trace when capturable |
| `report.md` | Full deliverable text, or report text when there is no deliverable |
| `result.json` | Final status, finding count or capture/timeout error |
| `client-error.json` | Client/HTTP error if the command cannot finish |

`detail.json` and progress are written during the run, so they remain if watching is
interrupted. Exit 0 means COMPLETED with retained findings and a captured report. Exit 1 means
failure or incomplete report capture; exit 2 means waiting for clarification, watch timeout or
interrupted watching. None of these is a factual-quality certification.

## Watch an existing run without submitting another

```sh
uv run --no-sync python -m contextforge.evals.api_research watch \
  --chat CHAT_ID --request REQUEST_ID --out-dir .data/runs/existing-run
```

This uses GET only. Stopping the watcher or reaching its timeout does not cancel the research.
The backend continues; use `watch` again with the saved IDs. A WAITING_USER run can be answered
through the UI or the existing replies API. If a POST response was lost, `run.json` preserves
the chat and idempotency key; resubmit the same question with `run --chat CHAT_ID
--idempotency-key SAVED_KEY` in a new output folder to resolve it through the normal idempotent
request route. No POST is automatically retried by this client.

Use `--chat CHAT_ID` on `run` for a follow-up in an existing conversation. Use `--out-dir PATH`
for a chosen new output folder, and `--case web-research-001` to identify a benchmark capture.
Keep reference answers out of the submitted prompt. Captured bundles can then be graded using
[the research evaluation workflow](evals/web-research-running.md).

## What has been verified

The integration test submits through the actual API routes and background worker using a
scripted model and fake web responses, then checks progress, evidence capture, idempotent
submission and the UI's chat/message read routes. It also checks stale-build refusal, missing
authentication and a read-only watch timeout. These are offline software checks.

This agent session's shell cannot connect to or bind server sockets, and computer use denied
Terminal access. The client is implemented for a normal local terminal; a successful live
research run from this restricted session has not been claimed.
