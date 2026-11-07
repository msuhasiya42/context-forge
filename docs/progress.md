# ContextForge: progress and next steps

Updated: 7 November 2026. This is the living status document. Each working session appends to the log at the bottom and updates the checklist. The design is in [system-design.md](system-design.md); the build plan is in [implementation-plan.md](implementation-plan.md).

## Where things stand

Phase 0, design contracts, and phase 1, the local engine, are complete (7 Nov 2026, branch `phase-1-engine`, 75 tests). The engine runs a request end to end on a scripted fake model and the fixture `sql_query` tool, with SQLite checkpoints, clarification resume, crash restart, duplicate protection and compaction. A frontend was built early in the sibling repository `context-forge-frontend` against a mock API client. Milestone 2 (phases 2, 3, 4, 7, 10) is being planned; see the 7 Nov phase-close log entry. AWS resources exist in Sydney (table below) but phases 1 to 9 do not use them.

Decided and documented:

- Stack, boundaries and ownership (system-design sections 2, 4).
- Vocabulary (system-design section 3).
- Nine entities with every field, Message shapes, working brief (implementation-plan sections 3 to 5).
- DynamoDB single-table physical design (implementation-plan section 7).
- Context flow, compaction policy, tool result processing (implementation-plan section 8).
- Model split and worked planner and execute calls (implementation-plan section 9).
- Stage prompts v1 (implementation-plan section 10).
- API and SSE contract with the 24 user expectations (implementation-plan section 11).
- Phase 1 task list with done criteria (implementation-plan section 13) and the task-by-task plan at `docs/superpowers/plans/2026-11-04-phase-1-engine.md`.
- The golden request `golden-001` (`evals/cases/golden-001.md`) and the eleven-phase split (system-design section 14).

## Decisions confirmed 4 November 2026

All eight defaults below were confirmed by Mayur and are recorded in the decisions log (system-design section 13). Build on them.

| # | Decision | Confirmed |
|---|---|---|
| 1 | Repository and tooling | Python 3.12, `uv` with lockfile, `pytest`, `ruff`, `mypy`; packages `core`, `worker`, `api` |
| 2 | What phases 1 to 9 run against | SQLite checkpointer, in-memory repositories, local disk storage; no AWS until phase 10 |
| 3 | First model provider | Scripted fake model in phase 1; then Bedrock only (confirmed 4 Nov 2026: Bedrock, DynamoDB, S3, S3 Vectors, Titan embeddings), after verifying model access in the account and region |
| 4 | Compaction numbers | Task subgraphs isolated; 40k cap per call; last 6 messages verbatim; all config |
| 5 | First fixture | 60-row synthetic dataset, two transfers and one card settlement planted; the spending request |
| 6 | Waiting badge (expectation 8) | `Chat.lastRequestStatus`, worker-written; built in phase 7 |
| 7 | Tool-call activity (expectation 15) | Transient `activity` SSE event, not persisted; built in phase 4 |
| 8 | Standing instructions (expectation 22) | `User.instructions` injected into the memory section; `agent-memory.md` stays system-owned; built in phase 7 |

Still open, not blocking: AWS compute and hosting choice; identity service; credential store for connections (secret manager versus KMS-encrypted attribute); the first ten evaluation requests and LangSmith key (both phase 2); region reverification, $5 usage alert and MFA before any AWS provisioning.

## Phase checklist

Phases are defined in system-design section 14 (revised 4 Nov 2026 for learning value; eleven phases since 6 Nov 2026).

| Phase | Status |
|---|---|
| 0. Design contracts | Done 4 Nov 2026 |
| 1. Engine, local | Done 7 Nov 2026 |
| 2. Real model, tracing, first eval | Not started |
| 3. Spreadsheet slice | Not started |
| 4. Thin API and UI | Not started |
| 5. PDF retrieval | Not started |
| 6. Public MCP (fetch, DuckDuckGo, time; `web_search`) | Not started |
| 7. Memory | Not started |
| 8. OAuth MCP and our own server | Not started |
| 9. Context stress, cost, latency, incident | Not started |
| 10. AWS persistence | Not started |
| 11. Writes and hosting | Not started |

## AWS resources (created 6 November 2026, Sydney ap-southeast-2)

Account 441186132531, project "Proof of Concept". Mumbai is unavailable on the account's plan; Sydney was chosen instead. Created through the console and CloudShell, to be imported into CDK in phase 11.

| Resource | Name | Settings |
|---|---|---|
| DynamoDB table | `contextforge` | PK, SK strings; on-demand; GSI1 on GSI1PK, GSI1SK projecting indexStatus, kind, name, chatId; stream on, NEW_IMAGE; point-in-time recovery on; deletion protection on |
| SQS queue | `contextforge-work` | standard; visibility timeout 1200 s; dead-letter after 3 receives |
| SQS dead-letter queue | `contextforge-work-dlq` | retention 14 days |
| S3 bucket | `contextforge-441186132531-apse2` | private, ACLs disabled, versioning off; lifecycle rule `expire-raw-results`: prefix `raw_result/` expires after 30 days |
| Bedrock | no setup needed | models auto-enable on first invoke; Claude 5.5 via inference profiles `au.anthropic.claude-opus-5-5`, `au.anthropic.claude-haiku-5-5` (or `global.`); `amazon.titan-embed-text-v2:0` on-demand in-region |
| IAM policy and user | `contextforge-dev` | created by Mayur in CloudShell; local CLI profile `contextforge` configured; verified read and write access to the table, queue and bucket |

Not created on purpose: the stream-to-SQS Lambda and service roles (phase 11, CDK).

## Legacy files

`bridge.py`, `build_template.py`, `test_bridge.py`, `verify_delivery.py` and `prototype.template.json` are an earlier, narrow DynamoDB-to-SQS event-delivery prototype using the old Job terminology. They are not the application and are not integrated. Keep them as reference until phase 10 deliberately replaces or removes them. `evidence/budget-created.jpg` records the $5 AWS usage alert.

## Rules for whoever picks this up

- Read system-design first, then implementation-plan. Do not reopen decisions in the decisions log (system-design section 13) without recording a new dated entry there.
- Add a field only with a requirement next to it. Fields listed as removed stay removed unless a new reason is recorded.
- Tests before implementation for every behavior in the phase 1 task list. Do not report a mocked test as proof of cloud delivery.
- No AWS resources, paid model calls, or external writes without an explicit go-ahead recorded in the log.
- Append a dated entry to the log below at the end of every session: what was done, what was verified, what is next.

## Log

- **4 Nov 2026.** Design review completed over one long session. Entity list settled at nine after dropping TranscriptMessage and KnowledgeBase and folding Report and ProposedAction into Message. Fields reviewed one by one. DynamoDB chosen over RDS; physical design written. Working brief, compaction policy, context flow and worked prompt examples written. Cimba integration design reviewed in Box; four rules adopted. Repository initialized; nine design documents consolidated into three. Next: confirm the five open decisions, then start phase 1 task 1.
- **4 Nov 2026, later.** API and SSE contract written (implementation-plan section 11) with the 24 user expectations; `decision` added to action_response. Phases reordered for interview readiness after checking the ranked top-20 question list in Box; evals moved to phase 2, AWS to phase 9. Open decisions 6 to 8 added. Next: confirm open decisions 1 to 8, then phase 1 task 1.
- **4 Nov 2026, end of session.** All eight open decisions confirmed. `User.instructions`, `User.updatedAt` and `Chat.lastRequestStatus` added to the entity tables. Committed. Next: Codex starts phase 1 task 1 (project skeleton) from implementation-plan section 13.
- **4 Nov 2026, LLD.** Module map and layering rule written (implementation-plan section 12). End-to-end walk of one request (9.3) exposed four defects (planner re-call on resume, task replay on restart, duplicate raw_result on replay, no cancel path); all nine review fixes applied to the phase 1 plan at `docs/superpowers/plans/2026-11-04-phase-1-engine.md` and recorded in the decisions log. Open decisions 2 to 16 from the 4 Nov list remain; next is the execution approach for phase 1.
- **5 Nov 2026.** Grill session (grill-with-docs run by hand): sixteen decisions on limits, behaviour and structure recorded in the decisions log; five glossary terms added; `facts` added to Task.result. Orchestration moves to the LangGraph Functional API; phase 1 plan tasks 8 to 10 to be rewritten accordingly before build. Next: backend runtime architecture, then rewrite the plan, then start phase 1.
- **6 Nov 2026.** AWS setup through Claude in Chrome (Brave) and CloudShell. Mumbai unavailable on the account plan; Sydney chosen. Created the DynamoDB table with GSI1, stream, PITR and deletion protection; two SQS queues; the S3 bucket with the raw_result lifecycle rule. Bedrock needs no model-access step. The IAM policy and user creation was blocked by the Claude Code permission classifier (credential grant), so Mayur runs those commands himself. Next: Mayur creates the IAM user and an access key, configures the `contextforge` CLI profile, then phase 1 task 1.
- **6 Nov 2026, later.** IAM user created and the `contextforge` profile verified against the table, queue and bucket. Go-ahead given for a paid smoke test (one Titan call, one Haiku call); both were refused by AWS with "your account is currently being verified, normally under 2 hours". No charge. Retry the smoke test before phase 2; phase 1 needs no AWS.
- **6 Nov 2026, planning closed.** Recorded the eleven-phase split (public credential-free MCP as phase 6 with `web_search`, `auth.kind: none`, `url` locator) and the golden request `golden-001` as the headline case and per-phase yardstick. Rewrote phase 1 plan tasks 8 to 10 around the LangGraph Functional API: stages as plain functions wrapped as tasks, interrupt in the entrypoint only, dependency waves with five in flight, crash restart with `None`, grill limits in `config/budgets.yaml`; added the facts field to TaskResult and seven review-focus items. Bedrock smoke test still pending account verification. Next: phase 1 task 1 (project skeleton) once Mayur says go.
- **6 Nov 2026, conventions.** Recorded the naming conventions (implementation-plan 12.1), the Context Builder stage-by-section table (8.2) and the tool inventory (12.2): one tool, `sql_query`, is coded in phase 1. Next: phase 1 task 1 on go.
- **7 Nov 2026.** Reviewed Cimba system-design docs 24, 25 and 26 (deep-research agent design, DR loop internals and context engineering, AI cost engineering) through three delegated full reads. Recorded 24 adopted changes across system-design sections 5 to 8, 10, 11, 15 and the decisions log, implementation-plan sections 2, 3, 8, 9 and 12, the golden case, and the phase 1 plan; listed what was not copied and why. Published two reference pages: the LLM design walk of golden-001 and the backend design-patterns map. Next: phase 1 task 1 on go.
- **7 Nov 2026, phase 1 complete.** All twelve tasks of `docs/superpowers/plans/2026-11-04-phase-1-engine.md` are built and reviewed on branch `phase-1-engine` (Task 11 ran as 11a and 11b). Final run: 75 tests pass; `ruff check`, `ruff format --check` and `mypy src` (strict) are clean.

  *Verified against the phase 1 done criteria (implementation-plan section 13):*
  1. End to end on the fake model and fake tool: the fixture request produces six Task records and one report Message with claims; independent tasks run concurrently; the planted transfer pairs are found by the fixture `sql_query` (`tests/test_workflow.py`).
  2. Restart: a run killed mid-way is requeued and completes without re-paying finished tasks (`test_restart_after_crash_does_not_replay_finished_tasks`).
  3. Resume: a clarification interrupt is answered by messageId and the run completes; the pending interrupt carries `nextAction`; a reply aimed at the wrong target is refused (`tests/test_runner.py`).
  4. Duplicate delivery: two deliveries of one request start one run (conditional ACCEPTED to RUNNING); two submits with one client key create one Message (`test_duplicate_start_runs_once`, `test_submit_is_idempotent_on_client_key`).
  5. Compaction: levels 1 and 3 keep constraints, facts and `nextAction` across repeated compactions, never split a tool call from its result, and a whole request completes under a small context cap (`tests/test_compaction.py`, `test_request_completes_under_a_small_context_cap`).

  *Deviations decided during execution* (beyond those listed at the end of the plan):
  - `config/models.yaml` is `{priceVersion, prices, stages}` so stage entries and the price version never mix.
  - `ModelCall.price_version` stays required, as spec 3.5 says; tests pass it.
  - `Claim` gains `status` (CONFIRMED, REJECTED, UNKNOWN; default UNKNOWN) and `value`; the report body gains `constraints` (text, source, date), matching the 7 Nov spec rows.
  - The `PlannerOutput` validator rejects dependency cycles, so a cyclic plan hits the planner retry with errors rather than the gate.
  - `AnyMessage` is imported from `langchain_core.messages` (the LangGraph re-export fails strict mypy).
  - `ContextInputs` carries `chat_id`, which the sources tag needs when there is no request.
  - A shaped tool result always keeps a raw_result artifact, also when only columns or cells were cut, so unshaped data is recoverable.
  - Shaping caps are never defaulted in code; callers pass the `budgets.yaml` values.
  - One token-counting rule: compaction and the Context Builder share `messages_tokens` from the builder.
  - A clarification moves the request status before saving the question Message, so a mid-call cancel cannot leave a question on a cancelled request.
  - Each wave starts at most `tasks_in_flight` ready tasks, in task_id order, so starts are ordered for replay.
  - Failed tool calls are never deduplicated; only `ok` results count as already done, so a retryable error can be retried.
  - DuckDB external access is disabled after the fixture load, so `sql_query` cannot read host files.
  - Restart detects an existing checkpoint with `checkpointer.get_tuple(config)`, since `get_state()` cannot tell a crashed run from a missing thread.
  - Compress calls use the executor model (the plan maps execute and compress to the same model); no separate compress model.
  - Task 11 was split: 11a (compaction module) ran in a worktree in parallel with Task 8; 11b wired it into the execute loop after Task 9.
  - The frontend was built early as a separate repository, `context-forge-frontend` (Vite, React 18, strict TypeScript, CSS Modules, mock API client until phase 4's API exists): chat list, chat view, composer, rename and archive. Part of phase 4's UI is therefore done.

  *Known gaps carried forward* (reviewer minors, deferred):
  - Stuck RUNNING tasks: a crash or cancel while a task is RUNNING leaves it (and the request) stuck; a reaper is phase 2.
  - Resume race: two racing resume deliveries can both save the reply before claiming; a lost claim race reports the wrong reason. Safe with one worker in phase 1.
  - Phase 4 boundary checks: resume does not check the reply's chat_id and request_message_id against its arguments; the resume payload is not validated.
  - Phase 2 adapter work: replace the `ScriptExhausted` catch with a provider-neutral `ModelError`; the Bedrock adapter must fold the mid-list level-3 summary SystemMessage into the preceding turn; compress ModelCalls carry no task_id and can exceed call_budget by one.
  - Phase 10: plan-gate Task writes are not batched, so a crash can leave a partial plan.
  - Test-coverage gaps: some validator shapes (diamond, 3-cycles, self-dependency), repository paths (update_status, on_save, ordering, concurrency), ScriptedModel locking, crash during a resumed run, the WAITING_USER tolerance branch, zero-ref fact grounding.
  - Docstring and tidy-up nits: stale docstrings in builder and shaping, CWD-relative config and prompt paths (run pytest from the repo root), unanchored ruff excludes for the prototype files, a shared dedup helper.

  *AWS, 7 Nov:* the account is on the Free Plan with $120 credits. Anthropic models are not available on the Free Plan (an upgrade is needed), so phase 2 defaults to Amazon Nova (Pro and Lite) through `config/models.yaml`, with Claude as the default once available; the Anthropic use-case form was submitted in the Bedrock console. Titan Text Embeddings V2 is confirmed in ap-southeast-2. Two of five credit activities are done (Budgets, Bedrock playground); Lambda, EC2 and RDS are pending.

  *Milestone 2:* phases 2, 3, 4, 7 and 10 next, with plans at `docs/superpowers/plans/2026-11-07-phase-*.md` (being drafted); phases 5, 6, 8, 9 and 11 later. Next: merge `phase-1-engine`, then phase 2 once its plan is reviewed.
