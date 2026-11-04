# ContextForge: progress and next steps

Updated: 4 November 2026. This is the living status document. Each working session appends to the log at the bottom and updates the checklist. The design is in [system-design.md](system-design.md); the build plan is in [implementation-plan.md](implementation-plan.md).

## Where things stand

Phase 0, design contracts, is complete and all open decisions are confirmed. Phase 1 task 1 can start. Nothing has been built: no application code beyond an old prototype, no AWS resources, no git history before 4 November 2026.

Decided and documented:

- Stack, boundaries and ownership (system-design sections 2, 4).
- Vocabulary (system-design section 3).
- Nine entities with every field, Message shapes, working brief (implementation-plan sections 3 to 5).
- DynamoDB single-table physical design (implementation-plan section 7).
- Context flow, compaction policy, tool result processing (implementation-plan section 8).
- Model split and worked planner and execute calls (implementation-plan section 9).
- Stage prompts v1 (implementation-plan section 10).
- API and SSE contract with the 24 user expectations (implementation-plan section 11).
- Phase 1 task list with done criteria (implementation-plan section 13).

## Decisions confirmed 4 November 2026

All eight defaults below were confirmed by Mayur and are recorded in the decisions log (system-design section 13). Build on them.

| # | Decision | Confirmed |
|---|---|---|
| 1 | Repository and tooling | Python 3.12, `uv` with lockfile, `pytest`, `ruff`, `mypy`; packages `core`, `worker`, `api` |
| 2 | What phases 1 to 8 run against | SQLite checkpointer, in-memory repositories, local disk storage; no AWS until phase 9 |
| 3 | First model provider | Scripted fake model in phase 1; then Bedrock only (confirmed 4 Nov 2026: Bedrock, DynamoDB, S3, S3 Vectors, Titan embeddings), after verifying model access in the account and region |
| 4 | Compaction numbers | Task subgraphs isolated; 40k cap per call; last 6 messages verbatim; all config |
| 5 | First fixture | 60-row synthetic dataset, two transfers and one card settlement planted; the spending request |
| 6 | Waiting badge (expectation 8) | `Chat.lastRequestStatus`, worker-written; built in phase 6 |
| 7 | Tool-call activity (expectation 15) | Transient `activity` SSE event, not persisted; built in phase 4 |
| 8 | Standing instructions (expectation 22) | `User.instructions` injected into the memory section; `agent-memory.md` stays system-owned; built in phase 6 |

Still open, not blocking: AWS compute and hosting choice; identity service; credential store for connections (secret manager versus KMS-encrypted attribute); the first ten evaluation requests and LangSmith key (both phase 2); region reverification, $5 usage alert and MFA before any AWS provisioning.

## Phase checklist

Phases are defined in system-design section 14 (revised 4 Nov 2026 for learning value).

| Phase | Status |
|---|---|
| 0. Design contracts | Done 4 Nov 2026 |
| 1. Engine, local | Not started. Tasks 1 to 10 in implementation-plan section 13 |
| 2. Real model, tracing, first eval | Not started |
| 3. Spreadsheet slice | Not started |
| 4. Thin API and UI | Not started |
| 5. PDF retrieval | Not started |
| 6. Memory | Not started |
| 7. MCP both ways | Not started |
| 8. Context stress, cost, latency, incident | Not started |
| 9. AWS persistence | Not started |
| 10. Writes and hosting | Not started |

## Legacy files

`bridge.py`, `build_template.py`, `test_bridge.py`, `verify_delivery.py` and `prototype.template.json` are an earlier, narrow DynamoDB-to-SQS event-delivery prototype using the old Job terminology. They are not the application and are not integrated. Keep them as reference until phase 9 deliberately replaces or removes them. `evidence/budget-created.jpg` records the $5 AWS usage alert.

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
