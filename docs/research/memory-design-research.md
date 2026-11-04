# Memory design research

Verified 3 November 2026. First-party sources only. This is a design recommendation, not an implementation or provisioning record.

Subsequent decision: the user chose **Agent Memory**, initially one versioned procedural Markdown file. A dedicated memory collection is deferred. The MemoryEntry recommendation below is research history for a possible later learned-memory extension, not the current initial implementation. See [the consolidated plan](../system-design-and-implementation-plan.md).

## 1. Correct the taxonomy

LangChain distinguishes memory by scope: short-term state belongs to a thread; long-term memories cross threads. Within long-term memory, semantic means facts, episodic means experiences, and procedural means instructions. Semantic memory is not synonymous with embedding search. A single profile or a collection of memories are both possible designs. [LangChain memory overview](https://docs.langchain.com/oss/python/concepts/memory)

For this app:

| Concept | Example | Scope and proposed owner |
|---|---|---|
| Working scratchpad / brief | Current plan, completed tasks, missing refund evidence | One Job's graph state |
| Chat continuity | Prior question, accepted answer, user correction | Relevant Jobs in the same Chat |
| Semantic long-term memory | User-confirmed account aliases or preferred currency | User + default agent, across Chats |
| Episodic long-term memory | A verified past reconciliation and what helped resolve it | User + default agent, retrieved when useful |
| Procedural long-term memory | Rules for classifying transfers, citing sources, preparing task results | Versioned agent instructions; optional task-specific procedures |

"Long-term" is a scope/lifetime category, not a fourth content type alongside semantic, episodic, and procedural. Scratchpad persistence can survive a restart without making its contents appropriate for every future Chat.

One default agent may serve many Chats and concurrent Jobs. It can run parallel tool calls or isolated task branches; this does not require multiple user-configurable agent identities.

## 2. Persistence and context are separate

LangGraph checkpointers persist thread execution state; stores persist cross-thread data in namespaces. An in-memory checkpointer loses its contents on process restart. Both systems can be used together. [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

Maintain the earlier Job-scoped workflow design: each Job has its own graph thread and scratchpad. A follow-up Job selects a relevant summary/results from earlier Jobs in its Chat, rather than sharing every active workflow's mutable state. Cross-Chat user memory is retrieved separately. Build each model context from selected state, not by serializing the entire checkpoint.

For a plan that intentionally spans Chats, persist an owned, versioned plan Artifact and a long-term memory reference to it. The active Job still has its own execution state. Avoid one globally mutable scratchpad shared by simultaneous Jobs.

## 3. Is one Markdown file enough?

There are real first-party file-based patterns. Deep Agents supports memory files with configurable storage backends, loading at startup or on demand, and different agent/user scopes. Read-only developer policies need not be writable learned memory. This describes the Deep Agents harness, not behavior enabled automatically by installing LangGraph. [Deep Agents memory](https://docs.langchain.com/oss/python/deepagents/memory)

Claude Code separates human-authored instructions from learned auto-memory, with scoped files and more detailed material loaded when relevant. Its docs emphasize concise instructions and note that instructions are guidance, not enforcement. [Claude Code memory](https://code.claude.com/docs/en/memory)

The Claude API memory tool is client-side: the model asks for file operations, while the application supplies storage and executes them. Its path is an abstraction that can map to database keys or other storage. Provider switching therefore does not require abandoning application-owned memory. [Claude memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool)

Recommendation: use Markdown for small, versioned default instructions and import/export, but not as the only canonical store for all growing user facts and episodes. A physical file is not the key issue; memory needs identity, ownership, relevance, provenance, revision, and deletion regardless of storage format. Uploaded Markdown is initially an Artifact. Importing it as memory must classify its sections and scope; it must not silently replace developer rules or grant tool permissions.

## 4. Minimal logical addition

Recommend a 13th logical entity, **MemoryEntry**, because cross-Chat retrieval, revision, and deletion are new requirements. It does not require a separate deployed service or DynamoDB table per memory type. The prior 12 entities remain valid for execution; Scratchpad, SemanticMemory, EpisodicMemory, and ProceduralMemory need not become four additional entities.

Suggested envelope:

```text
MemoryEntry
  memoryId
  ownerUserId / tenantId if introduced
  agentId = default-agent
  kind = semantic | episodic | procedural
  scope = user | agent-policy
  content or artifactRef
  sourceRefs
  revision
  status = candidate | active | superseded | deleted
  createdAt / updatedAt
  validFrom / expiresAt? / lastVerifiedAt?
```

Shared agent-policy content is developer-controlled and read-only to ordinary memory tools. User-derived memories are private to that user, even when the app has only one agent. Do not turn one user's preferences into global instructions. An Agent entity is not necessary solely to carry a fixed agent identifier.

## 5. AWS-compatible implementation path

The official `langchain-ai/langchain-aws` package documents **DynamoDBSaver** with optional S3 checkpoint offload and **DynamoDBStore** for persistent documents. Prefer evaluating these integrations over automatically writing custom persistence. Pin the actual package version and smoke-test persistence, async invocation, interruption/resume, and deletion before adoption. [AWS persistence package](https://github.com/langchain-ai/langchain-aws/blob/main/libs/langgraph-checkpoint-aws/README.md)

DynamoDBStore supports hierarchical namespaces, CRUD, TTL, basic equality-filtered search, and async equivalents. Without a vector index, search lists a namespace with optional filters; it does not provide semantic ranking. Its documented constraints include client-side filtering, namespace listing scans, no multi-item transactions, and DynamoDB's item limit. Optional vector search is separately configured and must not be assumed region/account compatible. [DynamoDBStore documentation](https://github.com/langchain-ai/langchain-aws/blob/main/libs/langgraph-checkpoint-aws/langgraph_checkpoint_aws/store/dynamodb/DynamoDBStore.md)

Proposed namespace: `(tenant_if_any, userId, default-agent, memoryKind)` constructed by trusted backend code. Do not accept an arbitrary user-supplied namespace. Start with direct lookup of a small preference profile and bounded retrieval by kind/tags. Add S3 Vectors retrieval later if memory volume and measured recall justify it; do not treat DynamoDB as automatically a vector database. Keep large memory/plan documents in private S3 and small references in the store.

The application still supplies memory approval policy, provenance, conflict resolution, retention/deletion, and concurrency/version checks. Verify the adapter's conditional-update behavior; ordinary `put()` is not proof that two concurrent edits cannot overwrite one another. Keep one authoritative storage path rather than duplicating a mutable MemoryEntry in two independent stores.

## 6. Memory lifecycle and examples

Start with explicit memory instructions: when the user says "remember my preferred currency is INR," save that preference with source and scope. For inferred memories, produce candidates and expose review/edit/delete rather than silently promoting every model-generated statement. Later consider bounded automatic consolidation only after accuracy tests.

- Durable preference: "Show money in INR unless asked otherwise."
- Conditional rule: "Exclude transfers between my confirmed owned accounts when calculating purchase-based spending." Preserve the definition and account evidence.
- Episode: "In the October reconciliation, merchant aliases caused a mismatch; account suffix evidence resolved it." Store the date, verified outcome, and source references as a relevant example, not a rule that all future matches are settled.
- Transient evidence: "This card bill is unpaid." Keep in dated Job results; re-query/reconcile for a new status question. Do not promote it into a timeless user fact.
- Never retain credentials or full card/account identifiers in general model memory. A memory stating permission cannot grant access; runtime authorization always decides.

Corrections supersede earlier revisions. Retrieval checks current active status/expiry in application code; TTL cleanup alone is not an immediate access/deletion guarantee. Delete source-derived search entries as well as canonical memory when appropriate.

## 7. Staged delivery and verification

1. Versioned default instructions, Job scratchpad, and durable checkpoints. No automatic learned memory yet.
2. Explicit semantic preferences across two Chats, with owner isolation, edit, and delete.
3. Episodic entries from verified completed Jobs, selected for similar requests; compare against a no-episode baseline.
4. Optional memory search/indexing, consolidation, and task-specific procedures only when useful.

Targeted tests: a preference survives restart and appears in another Chat; another user cannot retrieve it; a changed preference supersedes the old one; deleted/expired memory is not injected; compaction preserves constraints and uncertainty; an old payment episode is not presented as current status; two concurrent Jobs do not corrupt a shared profile; unrelated memories stay out of a model call. LangSmith can trace memory retrieval/write spans with IDs and redacted metadata, but is not the memory store or an evaluator of truth by itself.

```text
Versioned default instructions ─────────────┐
Relevant user MemoryEntries ────────────────┤
Selected Chat continuity ──────────────────┤
Job checkpoint / scratchpad ───────────────┤
Authorized task evidence + tools ─────────┤
                                         ↓
                                  Context Builder
                                         ↓
                                    Model call
                                         ↓
                        Validated explicit memory update
                                         ↓
                              Namespaced memory store
```

One-line decision: keep working plans per Job, share durable user memory across Chats through a scoped store, and keep default procedures small, versioned, and separately controlled.
