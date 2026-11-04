# ContextForge: entity model

Updated: 4 November 2026. This is the finalized logical model after a field-by-field review. It supersedes the sketches in [entity-model-handoff.md](entity-model-handoff.md), which keeps the review history. Terminology follows the [glossary](../GLOSSARY.md).

## Status

Nine logical entities: User, Chat, Message, Task, ModelCall, Artifact, Integration, IntegrationConnection, IntegrationTool.

Removed during review, with the reason:

| Former entity | Where it went |
|---|---|
| Job | Message of `kind=request` carries execution status |
| TranscriptMessage | LangGraph checkpoint state and LangSmith traces; remains a glossary term only |
| ProposedAction | Message of `kind=proposed_action` with a `payload` |
| Report | Message of `kind=report` with a `report` object; versioning was dropped, so a separate record had no job left |
| KnowledgeBase | Embedding settings are application config; `indexStatus` on Artifact says what is searchable |
| Finding, TokenUsage, MemoryEntry, Agent, Node, Edge, Checkpoint, Trace, Span | Never adopted; see decision history |

Memory adds no entity. Working memory is typed LangGraph state per request. Procedural memory is the versioned `agent-memory.md`. Semantic preferences are the small profile on User. Learned semantic and episodic records are a later `AgentMemoryRecord` extension.

Logical entities do not imply one DynamoDB table each. Physical keys and indexes come from the access patterns at the end. Every field cites a current requirement; add new fields the same way. Fields removed during review are listed under each entity so they are not re-added without a new reason.

Conventions: camelCase names; IDs are ULIDs, so creation order sorts lexically and no sequence fields exist; timestamps are ISO-8601 UTC; enumerated category fields are named `kind`; `status` is reserved for lifecycle state. `from` is not used because it is a Python keyword; the sender field is `sender`.

## Relationship diagram

```text
                              ┌────────────────────────────────────────┐
                              │                  User                  │
                              │  preferences { currency, timezone }    │
                              └──────┬──────────────┬───────────────┬──┘
                                    1│             1│              1│
                                    *│             *│              *│
                           ┌─────────▼────┐  ┌──────▼───────┐  ┌────▼──────────────────┐
                           │     Chat     │  │   Artifact   │  │ IntegrationConnection │
                           └─────────┬────┘  │ upload       │  └────┬──────────────────┘
                                    1│       │ dataset      │      *│
                                    *│       │ raw_result   │      1│
                    ┌────────────────▼────┐  │ deliverable  │  ┌────▼──────────┐1      *┌─────────────────┐
                    │       Message       │  └──▲────▲──────┘  │  Integration  ├────────►│ IntegrationTool │
                    │ kind=request        │     │    │         │  (config)     │         └─────────────────┘
                    │   owns status       │     │    │         └───────────────┘
                    │ kind=report         │     │    │ chatId: attachment lives in a chat
                    │   owns report{}     │     │    │ derivedFromArtifactId: dataset ← upload
                    │ kind=proposed_action│     │    │
                    │   owns payload,status     │    │
                    └──────────┬──────────┘     │    │
          requestMessageId     │                │    │
              ┌────────────────┼────────────┐   │    │
             *│               *│            │   │    │
        ┌─────▼─────┐   ┌──────▼─────┐      │   │    │
        │   Task    │   │  ModelCall │      │   │    │
        └─────┬─────┘   └────────────┘      │   │    │
              │ dependsOnTaskIds            │   │    │
              │ inputArtifactIds ───────────┼───┘    │   authorized sources, set by the planner
              │ result.evidenceRefs ────────┼────────┘   what was actually used: Artifact + locator
              │ result.artifactRefs         │
              └─────────────────────────────┘
        Message kind=report: report.claims[].evidenceRefs ──► Artifact + locator

 Related Messages (clarification, clarification_response, proposed_action, action_response,
 report, text) carry requestMessageId; replies also carry inReplyToMessageId.
 A proposed_action payload names the tool and account; the pending tool call itself lives in
 the LangGraph checkpoint for that request.
 Internal model and tool exchanges live in LangGraph checkpoints, not in any record.
```

Cardinality in words:

- A User owns many Chats, Artifacts and IntegrationConnections.
- A Chat has many Messages. Artifacts attach to a Chat through `Artifact.chatId`; Chat holds no list.
- A request Message has many Tasks, ModelCalls and related Messages, all through `requestMessageId`.
- A Task may depend on other Tasks in the same request and names the Artifacts it may read. ModelCalls optionally belong to a Task.
- Task results and report Messages point at evidence through EvidenceRef values: an Artifact plus a locator.
- An Integration has many IntegrationTools and many IntegrationConnections.

## Shared value types

Defined once and embedded, never stored as separate records.

```text
EvidenceRef
  artifactId          which stored source; Artifacts are immutable, so the id is the exact version
  locator             { kind: page | row | cell | range | message | section, value }
  excerpt?            short quoted text shown to the user

ArtifactRef
  artifactId
  role                input | output

Usage                 null when the provider did not report usage; unknown is never zero
  inputTokens
  outputTokens
  cacheReadTokens?
  cacheWriteTokens?
```

## Entities

### User

| Field | Required | Requirement it serves |
|---|---|---|
| userId | yes | Subject from the identity provider; owner key for every user-scoped record. Never accepted from a client body |
| email | yes | Account identification in the UI and connection matching; refreshed from the identity token on each login |
| displayName | no | Chat UI; refreshed on login |
| preferences | no | Small semantic profile: `currency`, `timezone`, `locale`. Applied by the Context Builder. Kept small because a growing profile becomes error-prone |
| status | yes | ACTIVE, DISABLED. A disabled user cannot start requests |
| createdAt | yes | Audit |

No learned facts or episodes on User; those arrive later as `AgentMemoryRecord` keyed by userId. Not included: lastSeenAt (nothing reads it), roles (one role), any credential.

### Chat

| Field | Required | Requirement it serves |
|---|---|---|
| chatId | yes | Identity |
| userId | yes | Owner; authorization and chat-list query |
| title | yes | Chat list; set by the API from the first request text, renamable by the user |
| status | yes | ACTIVE, ARCHIVED |
| createdAt | yes | Audit |
| updatedAt | yes | Chat list ordering; any write moves it, including rename |

Removed during review: lastActivityAt (updatedAt is enough for one user), attachment list (query Artifacts by chatId), summary (add only when a long chat measurably loses context), message count or preview (would duplicate Message content).

### Message

One record type with four shapes selected by `kind`. In code this is a Pydantic discriminated union; in DynamoDB it is one item type with conditional attributes. Absent attributes cost nothing.

Common fields, on every Message:

| Field | Required | Requirement it serves |
|---|---|---|
| messageId | yes | Identity. ULID gives display order. For `kind=request` it is also the LangGraph thread id and the execution identity |
| chatId | yes | Membership and ordered listing |
| userId | yes | Chat owner, copied so a read can be authorized without loading the Chat |
| sender | yes | user, assistant, system |
| kind | yes | request, clarification, clarification_response, proposed_action, action_response, report, text |
| content | yes | Visible text: the request, a question, a decision note, the report synthesis, or a note. Never updated after creation |
| requestMessageId | all but request | The request this entry belongs to; one query lists everything for a request |
| inReplyToMessageId | clarification_response and action_response | The exact entry being answered, since one request can raise several |
| createdAt | yes | Display and audit |
| updatedAt | yes | Last status change; equals createdAt on immutable kinds |

Shape `kind=request`, sender user. Only an entry explicitly classified as a request starts work; `sender == user` alone never does.

| Field | Required | Requirement it serves |
|---|---|---|
| status | yes | ACCEPTED, RUNNING, WAITING_USER, COMPLETED, FAILED, CANCELLED. A projection of graph state, not the scheduler |
| failureReason | when FAILED | User-facing explanation, no stack traces |

Shape `kind=report`, sender assistant. Exactly one per completed request, so a simple PDF question and a full spending comparison have the same evidence shape. A revision within the request is a newer report entry; the latest one is the current answer.

| Field | Required | Requirement it serves |
|---|---|---|
| report.claims | yes | `{ claimId, text, evidenceRefs[] }` per claim; claim-level evidence is the acceptance bar for answers |
| report.inputsConsulted | yes | ArtifactRefs the synthesis relied on, not every task artifact |
| report.deliverables | no | ArtifactRefs for generated files |
| report.uncertainties | yes | Unresolved items such as a refund with no matching credit; may be empty |
| report.coverage | yes | Periods compared, exclusions applied, date through which payment status was checked |

Shape `kind=proposed_action`, sender assistant. Created before any WRITE or DESTRUCTIVE tool runs. Integrations are read-only until phase 9.

| Field | Required | Requirement it serves |
|---|---|---|
| payload | yes | `{ tool, arguments, account }` exactly as shown to the user. Immutable; changed arguments mean a new proposed_action Message |
| status | yes | PROPOSED, APPROVED, REJECTED, EXPIRED, EXECUTING, EXECUTED, FAILED, UNCERTAIN. Approved is not executed |
| failureReason | when FAILED or UNCERTAIN | User-facing explanation |

The user's decision is a separate `kind=action_response` Message with `inReplyToMessageId`; its createdAt, userId and content are the immutable record of who decided, when, and why. `status` on the proposed_action entry is the live projection.

Shape for clarification, clarification_response, action_response and text: common fields only.

Who writes what: the API writes request, clarification_response and action_response entries. The worker writes clarification, proposed_action, report and text entries, and updates `status`, `failureReason` and `updatedAt`.

Removed during review: statusChangedAt (updatedAt), reportId (report is the Message), action.decidedBy/decidedAt/decisionNote (the action_response entry), action.expiresAt (code policy on createdAt), integrationToolId/integrationConnectionId (inside payload; the pending tool call is in the checkpoint), payloadHash (computed at resume from the checkpoint and the payload), executionAttempts (status plus LangSmith). Submission idempotency is a client key held in a short-TTL dedupe record at the API boundary, not a Message field.

### Task

| Field | Required | Requirement it serves |
|---|---|---|
| taskId | yes | Identity; ULID gives planning order |
| requestMessageId | yes | Owning request; listing tasks per request |
| kind | yes | sql_analysis, pdf_lookup, source_search, reconcile. Selects the validator for `result.structuredData` and the tool set |
| goal | yes | The planner's statement of what the task must establish |
| inputArtifactIds | yes | Artifacts this task is authorized to read, set by the planner. The plan gate verifies each one belongs to the request's chat |
| dependsOnTaskIds | no | Ordering and branch scheduling inside the graph |
| status | yes | PLANNED, RUNNING, COMPLETED, FAILED, SKIPPED, CANCELLED |
| attempt | yes | Distinguishes a transient retry from a replan; starts at 1 |
| result | when COMPLETED | `{ summary, structuredData, evidenceRefs[], artifactRefs[], uncertainties[], coverage }` |
| failureReason | when FAILED | Evidence-gap evaluation and user-facing reporting |
| createdAt | yes | Audit |
| updatedAt | yes | Progress display and stale-task reconciliation |

`inputArtifactIds` is what the task was allowed to use; `result.evidenceRefs` and `result.artifactRefs` are what it actually used or produced. The gap between them is itself a coverage signal. `structuredData` holds totals, matches and classifications, never row sets; rows go to a raw_result Artifact. `coverage` is task-kind specific. No chatId: tasks are reached through the request.

### ModelCall

| Field | Required | Requirement it serves |
|---|---|---|
| modelCallId | yes | Identity |
| requestMessageId | yes | Budget accounting per request |
| taskId | no | Attribution to a task branch |
| stage | yes | understand, clarify, plan, execute, summarize, evaluate, cite, compress, tool_select. Answers "where do the tokens go" from our own records, which survives masked traces |
| model | yes | Actual provider and model identity in one string, for example `bedrock/anthropic.claude-…` |
| contextVersion | yes | Agent memory version plus stage prompt version, for example `mem:3|prompt:plan.2` |
| usage | yes, nullable | Usage value type; null when the provider did not report it |
| estimatedCostUsd | no | Budget admission when tracing is unavailable |
| outcome | yes | COMPLETED, ERROR, TIMEOUT, CANCELLED |
| failureReason | when not COMPLETED | Diagnosis |
| langsmithRunId | no | Bridge into the trace for detail this record does not keep |
| startedAt | yes | Latency |
| endedAt | no | Latency; absent on timeout |

A replayed saved output never creates a ModelCall. Removed during review: attempt, promptVersion (folded into contextVersion), offeredToolIds, contextSnapshotArtifactId, redacted, stopReason, errorMessage (folded into failureReason). Tool-selection evaluation and exact-input replay are LangSmith's job in the early phases.

### Artifact

| Field | Required | Requirement it serves |
|---|---|---|
| artifactId | yes | Identity. Artifacts are immutable; a re-upload is a new Artifact |
| userId | yes | Owner and access scope |
| chatId | yes | The chat it was uploaded in. Raw results and deliverables take the chat of the request that produced them, so every Artifact lives under its chat |
| kind | yes | upload, dataset, raw_result, deliverable |
| name | yes | Display and citation |
| mediaType | yes | Ingestion routing and download |
| sizeBytes | yes | Validation and display |
| storageRef | yes | Private S3 key; never a public URL |
| status | yes | UPLOADING, AVAILABLE, FAILED, DELETED. Upload is two steps: the API registers and returns a signed URL, the client confirms completion |
| indexStatus | yes | NOT_INDEXED, PENDING, INDEXED, FAILED. Whether the embedding index covers this file |
| derivedFromArtifactId | no | A dataset produced by ingestion from an upload; used for display, regeneration and cascade delete |
| createdAt | yes | Audit |

A spreadsheet upload produces two records: the original (`kind=upload`) and the normalized Parquet dataset (`kind=dataset`, pointing back). SQL tasks read datasets only. Embedding model and chunking are versioned application config next to the agent memory file; a model change re-indexes everything.

Removed during review: contentHash (no version-pinning or dedupe requirement; S3 handles transfer integrity), supersedesArtifactId (returns in phase 4 with the newer-statement scenario), requestMessageId (Task.result.artifactRefs and report.deliverables already point the other way), producedByTaskId, datasetSchemaVersion (the dataset file carries its own schema), context_snapshot kind.

### Integration

Developer-maintained configuration checked into the repository, with secrets in a secret store. Kept in the model so that connections and tools have a target to reference.

| Field | Required | Requirement it serves |
|---|---|---|
| integrationId | yes | Stable key, for example `gmail` |
| name | yes | Display |
| transport | yes | `{ kind: mcp_http, mcp_stdio or native, serverUrl? }` |
| auth | yes | `{ kind: oauth2, api_key or none, authorizationEndpoint?, tokenEndpoint?, scopes[] }`; no secrets |
| clientCredentialRef | no | Secret store name for the OAuth client secret |
| enabled | yes | Switch a provider off without deleting it |

### IntegrationConnection

| Field | Required | Requirement it serves |
|---|---|---|
| connectionId | yes | Identity |
| userId | yes | Owner |
| integrationId | yes | Provider |
| externalAccountId | yes | Which account, for example the Gmail address, shown to the user |
| grantedScopes | no | What the provider granted; may be one coarse value for servers like Box |
| status | yes | ACTIVE, REVOKED. A revoked or expired grant surfaces as an error on the next call and flips the status |
| credentialRef | yes | Reference to the stored tokens. The store, secret manager or KMS-encrypted attribute, is chosen at phase 7 |
| createdAt | yes | When connected |
| updatedAt | yes | Status change |

Uniqueness: integrationId, userId, externalAccountId. Tokens never appear in this record, in graph state, or in anything a model sees; the worker resolves the reference at call time and discards the token afterwards. Removed during review: lastVerifiedAt, PENDING/EXPIRED/ERROR statuses.

### IntegrationTool

| Field | Required | Source | Requirement it serves |
|---|---|---|---|
| integrationToolId | yes | derived | `integrationId:name`; the pair is unique, so no separate ULID |
| integrationId | yes | system | Provider |
| name | yes | MCP | The server's own tool name, used on `tools/call`. The model-facing name `gmail_messages_search` is derived from integrationId plus name |
| description | yes | MCP, editable | What the model and the tool search read; the main lever for correct tool choice |
| inputSchema | yes | MCP | Validation before execution and pre-flight before a proposed_action |
| outputSchema | no | MCP, when sent | Result validation; absent for most servers |
| permission | yes | us | READ, WRITE, DESTRUCTIVE. Seeded from readOnlyHint, confirmed by a developer. WRITE and DESTRUCTIVE require a proposed_action Message |
| requiredScopes | no | us | Matched against the connection's grantedScopes; empty means any active connection |
| enabled | yes | us | Off until classified; set off, not deleted, when a re-sync no longer sees the tool |
| createdAt | yes | system | When discovered |

Uniqueness: integrationId, name. `tools/list` returns name, inputSchema, usually description, sometimes title, outputSchema and annotations; annotations are untrusted. Tool search ranks on name and description only; with twenty or thirty tools that is enough, and full schemas are loaded only for the selected tools. Removed during review: capabilityTags, aliases, companionToolIds, supportsPagination, supportsFiltering, schemaHash, lastSyncedAt, DEPRECATED/REMOVED statuses, isInternal.

## Rules recorded here rather than as fields

- Validate a proposed payload against the tool's inputSchema before creating the proposed_action Message. A card with a missing required field is never shown; the model asks the user first.
- Approval identity is the hash of the arguments, computed at resume from the checkpoint's pending call and the Message payload. Different arguments need a new approval; a retry of the same ones does not.
- A failed write becomes UNCERTAIN, and the model reads to verify before any retry, because the provider may have committed it.
- A PROPOSED action older than the configured window is treated as EXPIRED at resume time.
- Execution dispatch is triggered only by an explicit accepted-request event after the request Message is persisted; resume targets a verified pending interrupt.
- The planner may only name Artifacts in `inputArtifactIds` that belong to the request's chat; the plan gate enforces this in code.

## Working brief: the LangGraph state for one request

Not an entity and not a table. It is the state class every LangGraph graph must declare; the checkpointer saves it after each node into storage the library manages. It is read only by graph nodes, never by the API or the UI. It is listed here because compaction and resume depend on its shape.

| Field | Type | Written by | Requirement it serves |
|---|---|---|---|
| requestMessageId | string | set at start | Thread identity; links to the request Message |
| goal | string | understand node | The interpreted request in the agent's words; the raw text stays on the Message |
| constraints | list of strings | understand and clarify nodes | Pinned user rules such as "exclude transfers between my accounts". Compaction never touches them |
| plan | list of `{ taskId, kind, status }` | plan node, updated by execute | In-state copy of the task list so nodes do not query the database each step. Task records are the source of truth; rebuilt from them on resume |
| facts | list of `{ text, evidenceRefs[] }` | execute and evaluate nodes | Supported statements merged across tasks; the cite stage turns these into report claims |
| openQuestions | list of `{ text, resolvedBy? }` | understand, plan, evaluate nodes | What is still unknown and which task or clarification should settle it |
| nextAction | string | every node, on exit | One line stating what happens next; a resumed run reads this first |
| pendingInterrupt | `{ kind, messageId }` or null | clarify and propose nodes | Which clarification or proposed_action Message the graph is waiting on; resume verifies the reply targets it |
| messages | LangGraph message list | every model-calling node | The working transcript for the current loop; the only field compaction trims |

Rules:

- Compaction keeps every field except `messages`. A summary is appended to `messages`; the other fields are untouched.
- `plan` and `facts` are derived and can be rebuilt from Task records and results. `goal`, `constraints`, `openQuestions` and `nextAction` cannot, which is why they live in state.
- Nothing in state is shown to the user. If something should be visible, a node writes a Message for it.
- Per-request budget limits are checked by summing ModelCall records, not from a state counter.

## Lifecycle diagrams

```text
Message kind=request . status

  ACCEPTED ──► RUNNING ──┬──► COMPLETED
                 ▲       ├──► FAILED (failureReason)
                 │       └──► WAITING_USER ──► clarification_response or action_response resumes
                 └──────────────────┘
  any non-terminal ──► CANCELLED
```

```text
Message kind=proposed_action . status

  PROPOSED ──┬──► APPROVED ──► EXECUTING ──┬──► EXECUTED
             ├──► REJECTED                 ├──► FAILED
             └──► EXPIRED                  └──► UNCERTAIN ──► read to verify ──► EXECUTED or FAILED
```

```text
Task . status

  PLANNED ──► RUNNING ──┬──► COMPLETED (result present)
                ▲       └──► FAILED (failureReason)
                └── retry (attempt + 1)
  PLANNED ──► SKIPPED (dependency failed or planner dropped it)
  any non-terminal ──► CANCELLED
```

```text
Artifact . status                     Artifact . indexStatus

  UPLOADING ──► AVAILABLE ──► DELETED   NOT_INDEXED ──► PENDING ──┬──► INDEXED
      └──────► FAILED                                            └──► FAILED
```

## One request, end to end

```text
 1. User sends a request in Chat c1
    └─ API persists Message m1 (user, request, status=ACCEPTED)
    └─ accepted-request event dispatched, deduplicated by the client idempotency key
 2. Worker starts LangGraph thread_id = m1
    ├─ ModelCall (stage=understand) reads agent-memory.md vN, User.preferences, Chat attachments
    ├─ needs clarification → Message m2 (assistant, clarification, requestMessageId=m1)
    │                        m1.status = WAITING_USER; graph interrupted
    │   User replies       → Message m3 (user, clarification_response, inReplyToMessageId=m2)
    │                        resume API verifies the pending interrupt; m1.status = RUNNING
    ├─ ModelCall (stage=plan) → Task t1 sql_analysis inputArtifactIds=[a2]
    │                           Task t2 pdf_lookup   inputArtifactIds=[a4]
    │                           Task t3 reconcile    dependsOnTaskIds=[t1, t2]
    ├─ t1: SQL over dataset a2 (derivedFrom upload a1) → rows Artifact a3 (raw_result)
    │      result { summary, structuredData, evidenceRefs → a2 rows, artifactRefs → a3, coverage }
    ├─ t2: retrieval over a4 (indexStatus=INDEXED) → result with evidenceRefs → a4 page 3
    ├─ t3: reconcile → result with uncertainties
    ├─ ModelCall (stage=cite) attaches claims to EvidenceRefs
    └─ Message m4 (assistant, report, requestMessageId=m1) with report{claims, inputsConsulted, coverage}
       m1.status = COMPLETED
 Every ModelCall records stage, contextVersion and usage. SSE streams m1 status and m4.
```

## Access patterns

Listed before the keys, because in DynamoDB the questions shape the storage. Each one names what the caller already holds.

| # | Question | Asked by | Holds |
|---|---|---|---|
| 1 | List Chats for a user, most recently updated first | chat list | userId |
| 2 | List Messages for a Chat in order | chat screen | chatId |
| 3 | Get a request Message with its status | progress panel, resume API | chatId, messageId |
| 4 | List Tasks and ModelCalls for a request | progress panel, worker | requestMessageId |
| 5 | Pending proposed_action Messages for a request | resume API | chatId, requestMessageId |
| 6 | List Artifacts attached to a Chat; get one by id | chat screen, citation click | chatId, artifactId |
| 7 | List a user's Artifacts with indexStatus INDEXED | planner, cross-chat retrieval | userId |
| 8 | List IntegrationConnections for a user; list enabled IntegrationTools for an Integration | connection screen, tool search | userId, integrationId |
| 9 | Sum ModelCall usage per request | budget admission | requestMessageId |

Request URLs are chat-scoped, `/chats/{chatId}/requests/{messageId}`, so the API always holds the chatId and no lookup by messageId alone is needed.

## Physical design: DynamoDB

One application table, one sparse index, plus the LangGraph checkpointer's own tables. Integration stays a config file.

### Keys

| Entity | PK | SK | Patterns served |
|---|---|---|---|
| User | `USER#<userId>` | `PROFILE` | get by id |
| Chat | `USER#<userId>` | `CHAT#<chatId>` | 1, and get with owner check in one read |
| IntegrationConnection | `USER#<userId>` | `CONN#<integrationId>#<externalAccountId>` | 8; uniqueness is in the key |
| Message | `CHAT#<chatId>` | `MSG#<messageId>` | 2, 3, 5 |
| Artifact | `CHAT#<chatId>` | `ART#<artifactId>` | 6 |
| Task | `REQ#<requestMessageId>` | `TASK#<taskId>` | 4 |
| ModelCall | `REQ#<requestMessageId>` | `CALL#<modelCallId>` | 4, 9 |
| IntegrationTool | `INT#<integrationId>` | `TOOL#<name>` | 8; uniqueness is in the key |
| Idempotency record | `IDEMP#<clientKey>` | `REQ` | submit deduplication; `ttl` attribute, 24 hours |

Why these groupings: Messages and Artifacts are always read together when a chat opens, so they share the chat partition. Tasks and ModelCalls are always read together when a request is inspected, so they share the request partition. Each partition is bounded by one chat or one request, never by the user's whole history.

Why these sort keys: the prefix separates entity types within a partition, and the ULID orders items by creation, so `begins_with(SK, "MSG#")` returns a chat's messages in display order with no sort step.

Ids inside a key are stored again as plain attributes, for example `requestMessageId` on a Task, so code never parses key strings. Ids never change, so the copy cannot go stale.

### Index

`GSI1`, sparse, Artifact items only: `GSI1PK = USER#<userId>`, `GSI1SK = ART#<artifactId>`, projecting `indexStatus`, `kind`, `name`, `chatId`. Serves pattern 7, the one question that cannot start from a partition the caller holds, because a user's files are spread across many chats. No other item carries these attributes, so the index holds Artifacts only.

Pattern 1 ordering is done in memory after querying the user partition. One user has hundreds of chats at most; an index for it is not justified.

### Reads

- Patterns 1, 2, 4, 6, 8: Query on one partition, optionally `begins_with` on SK.
- Pattern 3, 6 by id: GetItem.
- Pattern 5: Query the chat partition with a filter on `kind = proposed_action` and `status = PROPOSED`. The filter runs after the read; a chat partition is small enough for that.
- Pattern 7: Query GSI1 with a filter on `indexStatus = INDEXED`.
- Pattern 9: Query the request partition for `CALL#` items and sum `usage` in code.
- No Scan anywhere in the application.

### Writes that carry a condition

| Write | Guarantee | How |
|---|---|---|
| Submit a request | A replayed submission does not create a second request | TransactWriteItems: put `IDEMP#<clientKey>` with `attribute_not_exists(PK)`, and put the request Message. On failure, return the existing messageId |
| Status change on request or proposed_action Message, Task, Artifact | A stale worker cannot overwrite a newer state | UpdateItem with `ConditionExpression: #status = :expected` |
| Create IntegrationConnection or IntegrationTool | One per key | PutItem with `attribute_not_exists(SK)` |
| Complete an upload | Only an UPLOADING artifact becomes AVAILABLE | UpdateItem with `ConditionExpression: #status = :uploading` |

### Sizes, growth and expiry

- Item limit is 400 KB. A report Message with content and claims is expected under 50 KB; cap at 100 KB, beyond which content moves to a deliverable Artifact and the field becomes a reference. Task.result holds totals and references, never row sets.
- Partitions are bounded by one chat or one request.
- Only the idempotency record has a TTL. Entities are never expired by the database.

### Operational settings

- On-demand capacity: usage is bursty and tiny; nothing is billed when idle.
- Streams on, new images. The dispatcher Lambda filters for INSERT where SK begins with `MSG#` and `kind = request`; that insert is the accepted-request event. Resume goes from the API to SQS directly, not through the stream.
- Point-in-time recovery on.
- Default encryption at rest.
- LangGraph checkpoints live in the tables DynamoDBSaver creates and owns, with S3 offload for large state. Application code never reads them.

### Sample items

The same request as the end-to-end walk, as stored.

```text
PK          SK            attributes
USER#u1     PROFILE       email=mayur@…  preferences={currency:INR, timezone:Asia/Kolkata}  status=ACTIVE
USER#u1     CHAT#c1       title="Compare Oct vs Sep spend"  status=ACTIVE  updatedAt=…
CHAT#c1     MSG#m1        sender=user  kind=request  status=RUNNING  content="Compare October…"
CHAT#c1     MSG#m2        sender=assistant  kind=clarification  requestMessageId=m1  content="Should card bill…"
CHAT#c1     MSG#m3        sender=user  kind=clarification_response  requestMessageId=m1  inReplyToMessageId=m2
CHAT#c1     ART#a1        kind=upload   name=october.xlsx  status=AVAILABLE  indexStatus=NOT_INDEXED  GSI1PK=USER#u1  GSI1SK=ART#a1
CHAT#c1     ART#a2        kind=dataset  name=october.parquet  derivedFromArtifactId=a1  GSI1PK=USER#u1  GSI1SK=ART#a2
REQ#m1      TASK#t1       kind=sql_analysis  status=RUNNING  inputArtifactIds=[a2]
REQ#m1      TASK#t2       kind=reconcile     status=PLANNED  dependsOnTaskIds=[t1]
REQ#m1      CALL#k1       stage=plan  model=bedrock/…  usage={inputTokens:4200, outputTokens:610}
```

### Repository rule

No code outside the storage module builds a key string. Every entity exposes `save`, `get` and `list_by_parent` functions, for example `messages.list_by_chat(chatId)`. A new index, or a move to another store, touches one module.

## Later extension, not in the count

```text
AgentMemoryRecord
  memoryId, userId, kind: semantic | episodic
  content or artifactRef, topic/tags, sourceRefs (EvidenceRef[])
  revision, status: candidate | active | superseded | deleted
  createdAt, updatedAt, lastVerifiedAt?, expiresAt?
```

Deferred fields with a known return point: Artifact.supersedesArtifactId and contentHash (phase 4, newer statement replaces older), Message report versioning if revisions ever need to coexist, IntegrationTool retrieval aids if measured recall fails.
