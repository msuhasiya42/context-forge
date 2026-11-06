# ContextForge: progress and next steps

Updated: 6 November 2026. This is the living status document. Each working session appends to the log at the bottom and updates the checklist. The design is in [system-design.md](system-design.md); the build plan is in [implementation-plan.md](implementation-plan.md).

## Where things stand

Phase 0, design contracts, is complete and all open decisions are confirmed. The phase 1 plan is final (Functional API) and phase 1 task 1 can start. Nothing has been built: no application code beyond an old prototype. AWS resources exist in Sydney (table below) but phases 1 to 9 do not use them.

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
| 1. Engine, local | Not started. Twelve tasks in `docs/superpowers/plans/2026-11-04-phase-1-engine.md` |
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
