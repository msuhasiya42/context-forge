# ContextForge: entity-model handoff

Updated: 4 November 2026.

## Status: superseded by the finalized entity model

On 4 November 2026 the entity list was fixed at 9 (User added; TranscriptMessage dropped in favour of LangGraph checkpoint state; ProposedAction and Report folded into Message as kinds proposed_action and report; KnowledgeBase dropped in favour of indexStatus on Artifact) and fields were finalized in [entity-model.md](entity-model.md). The sender field is `sender`, not `from`, and category fields are named `kind`. The sections below are preserved as the review history that led to that document.

## Earlier status: Message replaces Job; field schemas under review

The latest decision replaces the separate Job entity with Message. An initial user request Message represents the request and carries its execution status. Messages also represent visible answers, clarifications, and proposed-action presentations. Internal model/tool exchanges remain TranscriptMessages.

This supersedes the earlier Job question/answer model and the rule against a separate visible-message entity. Report and ProposedAction remain in the working list while their possible consolidation into Message is reviewed; that broader merge is not finalized. There are currently 12 working entities because Message replaces Job one for one. Requiredness, exact names/types, enums, and physical DynamoDB keys/indexes remain under review. Do not expand these sketches automatically into production-sized models.

## Message and request execution

Keep sender and purpose separate:

- `from` identifies the sender: user, assistant, or system.
- `type` identifies the purpose. Candidate values include request, text, clarification, clarification_response, proposed_action, and action_response; exact values remain under review.
- A new user Message explicitly classified as a request starts work. `from == "user"` alone is insufficient because users also answer clarifications and approve or reject actions.
- The initial request's `messageId` is the stable execution identity. Related Messages and execution records use `requestMessageId` to reference it. The initial Message does not need a second identity or a self-reference.
- Execution `status` belongs to the initial request Message; it is not required on every chat entry. Graph checkpoints remain authoritative for continuation.
- A clarification response or action decision resumes the same request and workflow. A new follow-up request after completion gets a new initial Message.
- Start dispatch uses an explicit accepted-request event; resume uses an authorized API operation targeting the pending interruption. A sender filter or an ordinary Message/status update must not automatically start execution.
- Duplicate event delivery and API retries must resolve to existing work using server-side deduplication and workflow execution ownership. A stable message ID alone does not make duplicate request submissions idempotent; the submission contract remains to be designed.

Small Message field sketch: `messageId`, `chatId`, `userId`, `from`, `type`, `content`; `requestMessageId` on related entries, and execution `status` on the initial request. Field names and conditional requiredness are still proposals. Timestamps, workflow correlation, clarification targeting, and any Report/ProposedAction reference structures still need field-by-field review. Do not reintroduce Job-level limits, source selection, or waitingFor by default.

Example relationship:

```text
Chat
  Message m1: user / request                  [owns execution status]
    Task t1: requestMessageId = m1
    Task t2: requestMessageId = m1
  Message m2: assistant / clarification       [requestMessageId = m1]
  Message m3: user / clarification_response   [requestMessageId = m1]
  Message m4: assistant / text                [requestMessageId = m1]
```

Messages own the visible conversation. Report may remain a separately versioned synthesis referenced by an answer Message; whether ordinary answers should replace Report entirely is open. ProposedAction may remain a separate operation referenced by a Message or become structured content within one; either way, exact payload, approval, and execution outcomes must remain distinguishable. Do not duplicate full answer/action content in competing mutable records.

## Entities and working field sketches

| Entity | Purpose | Fields discussed / working sketch |
|---|---|---|
| Chat | Groups related requests and visible Messages | chatId, userId, title, status, createdAt, updatedAt, lastActivityAt; attachment association still to review |
| Message | Visible chat entry; the initial request also represents its execution | messageId, chatId, userId, from, type, content; requestMessageId on related entries; execution status on initial request |
| Task | Bounded work within a request | taskId, requestMessageId, goal, status, createdAt, failureReason, result; dependencies still to review |
| TranscriptMessage | Ordered internal model/tool exchanges | Transcript ID, requestMessageId, optional taskId, sequence, role, content, tool-call linkage, createdAt |
| Report | Versioned final synthesis and evidence; consolidation into Message open | Report ID, requestMessageId, content/content reference, evidence references, output artifact references, version |
| ModelCall | One actual model invocation and usage | Call ID, requestMessageId, optional taskId, stage, provider/model, input/output usage, cost, outcome, context reference |
| ProposedAction | Concrete external operation and decision/execution; consolidation into Message open | Action ID, requestMessageId, tool, payload, payload hash, status, approval identity/time, execution outcome |
| KnowledgeBase | Reusable searchable source collection | ID, owner, name, source associations, retrieval configuration |
| Artifact | Source file, dataset, raw result or generated output | Artifact ID, owner, type, storage reference, source/version identity, producing request/Task when applicable |
| Integration | Provider-level configuration | Integration ID, provider/name, transport/endpoint configuration, authentication configuration |
| IntegrationConnection | User's authorized provider account | Connection ID, userId, Integration reference, account reference, scopes, status, secure credential reference |
| IntegrationTool | Discoverable provider operation | Tool ID, Integration reference, name, description, input/output schema, permission classification, schema version/hash |

Framework correlation fields are included only when they serve a demonstrated need, not copied into every entity. `requestMessageId` replaces the earlier `jobId` relationship in current designs; legacy prototype scripts and historical research are not migrated by this document.

## Chat and attachments

The Chat proposal remains: `chatId`, `userId`, `title`, `status`, `createdAt`, `updatedAt`, `lastActivityAt`. Candidate status values are ACTIVE and ARCHIVED. updatedAt represents metadata changes; lastActivityAt represents user-visible activity rather than every internal tool call.

Uploads become Artifacts stored in private S3 and are attached to the Chat. Requests in that Chat can use authorized attachments. Sources do not need to be copied onto each request Message. The physical attachment relationship remains to be reviewed; do not embed an unbounded collection of Message or Artifact IDs in Chat. Task results and reports still identify the actual evidence they used.

## Task result

Task owns its compact result, replacing the separate Finding entity. Candidate envelope:

```text
Task.result
  summary
  structuredData
  evidenceRefs
  artifactRefs
  uncertainties
  coverage
```

Large raw responses remain in Artifacts. Result validation and task-specific structures still need review.

## UI and transport direction

The UI has a Chat list on the left and visible Messages on the right. SSE delivers progress and answer updates; regular APIs submit requests and resolve clarifications/approvals. An initial request-scoped stream follows the board's design, using the initial Message ID as its execution identity. Exact endpoints and any later Chat-wide multiplexing remain open.

SSE disconnection does not cancel execution. Reopening a Chat loads persisted Messages and request status, then reconnects. Replaying missed events or refreshing current state requires server support. Internal transcripts are not automatically rendered as chat bubbles, and live progress does not require a new application entity.

## Decisions to preserve

- Message replaces Job. Do not create both records for the same initial request.
- Sender alone does not distinguish new work from clarification or action responses.
- No separate Finding; use Task.result.
- No separate TokenUsage; record usage/cost on ModelCall.
- No generic required schemaVersion on Chat/Message. Add explicit format versions only for a real incompatible-format coexistence requirement.
- Agent Memory is initially one versioned agent-memory.md procedural document, shared across Chats. No MemoryEntry table.
- Scratchpad is request-scoped working state persisted through LangGraph checkpoints, not a global mutable file shared by active requests.
- One default agent configuration; no separate Agent entity solely for that constant.
- No application entities for Node, Edge, Checkpoint, Trace or Span duplicating framework records.
- Task is domain work, not necessarily one graph node.
- Logical entities do not imply separate DynamoDB tables. Physical design follows access patterns.
- Reports reference relevant evidence and generated deliverables, not automatically every artifact from every Task.
- Approval is distinct from successful execution, whether action state is stored separately or embedded later.
- Keep credentials out of model context and ordinary records; connections use secure references.
- Explain each additional field with a current requirement. Avoid generic flags, counters and metadata by default.

## Next review

Review Message fields individually, then Task and the other entities. Settle whether Report and ProposedAction remain separate before fixing the entity count. Do not implement APIs, database tables, or AWS resources yet.

[Consolidated design and implementation plan](system-design-and-implementation-plan.md) contains broader context. Sections marked later, candidate or recommended are not finalized schema requirements. [Glossary](../GLOSSARY.md) defines the agreed terminology. Older architecture/research notes preserve historical Job terminology; this handoff takes precedence for the current entity model.
