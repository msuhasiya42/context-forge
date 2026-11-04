# ContextForge: design continuity

Updated: 4 November 2026 (Asia/Kolkata).

Project name: **ContextForge — AI Research Agent with Context Engineering and Persistent Memory**. Initial Chat/Message models should not require generic schemaVersion boilerplate; introduce explicit format versions only for a concrete incompatible-format coexistence requirement. This supersedes the earlier generic field suggestion.

Current consolidated reference: [system-design-and-implementation-plan.md](system-design-and-implementation-plan.md). Latest agreement: use the name **Agent Memory**, initially one versioned `agent-memory.md` procedural document across Chats. Replace Job with Message in the working entity list; defer a dedicated memory collection/table and learned fact/episode records. This supersedes the earlier MemoryEntry-as-13th-entity recommendation. The current reference also records the verified DynamoDBSaver/DynamoDBStore integration to evaluate, superseding earlier uncertainty about available AWS adapters. React, FastAPI/Python, LangGraph and LangSmith are fixed choices.

This note preserves the conversation's decisions and research-based proposals. It is a design record, not evidence that the described application is implemented or deployed. Continue design discussion before changing code, APIs, database schemas, or AWS resources.

## Latest entity decision: 4 November 2026

Message replaces the separate Job entity. The initial user request Message carries execution status and provides the stable execution identity; related records use requestMessageId. Sender and purpose are separate: from=user alone must not trigger work because clarification and action responses also come from the user. Only explicitly classified new requests start work; authorized responses resume the same request/workflow. Uploaded Artifacts belong to Chat. SSE carries live updates and APIs handle submission/resume.

Whether Report and ProposedAction also consolidate into Message remains under review. See the [entity-model handoff](entity-model-handoff.md) for the current working list and candidate fields. The Job terminology, no-ChatMessage rule, correlation names and entity counts below preserve the earlier discussion and are superseded where they conflict with this decision. The existing runtime prototype is unchanged.

## Agreed direction

- Learning and low cost are primary goals, especially context engineering, Python agents, AWS, and NoSQL modeling.
- React/TypeScript frontend and Python backend/agent workers; do not introduce Spring Boot.
- LangGraph is the core orchestration runtime inside the Python worker; LangSmith is the chosen tracing/evaluation system, integrated after the initial LangGraph workflow. Design correlation identifiers for both from the beginning. LangSmith integration and a Deep Agents SDK migration are not prerequisites for the first workflow.
- Favor DynamoDB for operational records and NoSQL learning, based on access patterns rather than JSON shape alone. PostgreSQL JSONB remains a valid alternative; nested data does not mandate NoSQL.
- Bedrock is the intended model provider. RAG is built explicitly to learn ingestion, chunking, retrieval, context, and citations; AWS S3 Vectors is the intended AWS target, with local Chroma an optional development alternative. Verify model/service regional availability and current pricing before provisioning.
- Uploaded spreadsheets are analytical datasets, separate from operational Job/Task records. Private object storage and isolated DuckDB querying are the proposed initial analysis path; Athena can be evaluated later.
- Integration/MCP coordination, Gmail and Drive access are future capabilities. Begin with read-only behavior and synthetic fixtures. Never infer an unpaid bill solely from a reminder or a paid bill solely from an equal-amount transaction.
- One region. Earlier console inspection found a project account assigned Sydney; Mumbai was an initial preference, not an authorization to change the plan or enable advanced features. Reverify console facts before action. AWS implementation remains paused.

## Baseline entity agreement: 12 logical entities before cross-chat memory

Logical entities do not imply one DynamoDB table or deployed service per entity.

1. Chat: groups related requests and follow-ups.
2. Job: one user request and its execution lifecycle, initial question, final answer, constraints, and relevant state references.
3. Task: bounded work within a Job, dependencies, execution status, attempts, and an embedded structured result.
4. TranscriptMessage: ordered exchanges between participants during execution, including tool requests and responses or references; not a general log of every state transition or private model reasoning.
5. Report: optional versioned synthesis of results with evidence and optional generated deliverables.
6. ModelCall: one model invocation, including calls for planning, compression, or reporting; records usage/cost and context references. taskId can be absent for Job-level calls.
7. ProposedAction: concrete external operation and its approval and execution information. Approved does not mean executed.
8. KnowledgeBase: reusable collection of sources and retrieval configuration.
9. Integration: provider-level configuration.
10. Artifact: stored source, normalized dataset, raw result, or generated output, represented by metadata and a storage reference.
11. IntegrationConnection: user's provider account, scopes/status, and a secure credential reference.
12. IntegrationTool: provider operation and its schema and permission classification.

Finding was removed as an independent entity for the first version. Supported observations can live in Task.result. No separate ChatMessage initially: the UI can display Job question/answer, with ordered clarification exchanges within the Job if needed. Revisit if independent message lifecycle, edits, branching, or reruns require it.

No separate Scratchpad, ContextSnapshot, TokenUsage, or AuditEvent entity is required initially. Working state and context snapshots remain important logical concepts. Approval history can live with ProposedAction; a future full access audit is broader than approvals. Persist bounded execution attempts/outcomes before enabling external writes, with separate storage if they grow.

## Research-based proposals to discuss

These are recommendations from the latest research, not all individually approved schema fields or implementation decisions.

### LangGraph and LangSmith compatibility review

The existing 12 logical entities cover the initial application. Do not add domain entities for Node, Edge, Checkpoint, Trace, or Span merely to duplicate framework-managed records.

Ownership:

- Application records own user identity, source authorization, accepted requests/results, approvals and external operation outcomes, and usage needed for application budgets. Task is domain work, not necessarily a graph node or LangGraph's internal task object.
- LangGraph's configured checkpointer owns resumable graph state, node execution progress, checkpoint lineage, and pending interrupts. Graph working state is not reconstructed from LangSmith traces.
- LangSmith owns observability traces/runs. A LangSmith run is a span-like observed operation, not our Job entity. A paused/resumed Job can produce multiple root traces.

Initial correlation design proposal:

- Chat keeps chatId. Propagate chatId as trace metadata for filtering across Jobs; Chat need not equal a LangGraph thread.
- Job stores its active workflow reference, including langgraphThreadId and graphVersion. For the first version, a thread scoped to one Job is natural: use the same thread for retries/resume, and isolate parallel Jobs. Use a distinct execution reference for an intentional fresh rerun if that feature is introduced.
- Task keeps its own taskId and jobId, plus task type, dependency/input references, status/result, and optional workflow/subgraph correlation. Do not make taskId equal a reusable node name. Node names are useful observability metadata, not unique task identity.
- TranscriptMessage keeps stable message identity, scope (Job/Task or execution), role, content/Artifact references, ordering, and toolCallId links. Reuse framework message structures where possible. Choose one canonical message record and expose projections/exports rather than copying full message history into competing mutable stores.
- ModelCall keeps call identity, jobId/optional taskId, stage, invocation attempt, actual provider/model, usage/outcome, context reference, and optional langsmithRunId/traceId. Record from the same model boundary/event that emits traces; do not independently count parent trace totals and child call usage. Keep minimal authoritative usage needed for budgets even when tracing is disabled or unavailable; unknown provider usage remains unknown.
- ProposedAction ties the exact operation and decision to its workflow and pending interrupt identifier/reference when needed. The resume API authorizes the actor, checks current pending state and approved payload, records the decision, then resumes the matching workflow. Framework interrupt details do not replace the business approval record.
- Report/Artifact use stable application references; optional trace links aid navigation without changing ownership or exposing private content by default.

Use Job-scoped workflow/thread grouping initially, with chatId/jobId/taskId/modelCallId propagated as appropriate trace metadata. Preserve nested trace context across parallel/asynchronous work using supported instrumentation. Sampling, trace retention, or telemetry failures must not determine application correctness, billing-budget enforcement, authorization, or recovery. Selective redaction/disabled payload capture is required for real private data.

Checkpoint backend remains an explicit design decision. DynamoDB application entities do not automatically provide a LangGraph checkpointer. Start locally with an appropriate persistent backend, then select a supported/custom backend for deployment after checking current compatibility; no hosted LangGraph service or additional database is implicitly approved.

### Context controls inside the worker

- Build context before each model invocation, using its purpose, task scope, constraints, authorized/versioned sources, relevant results, selected tools, and token/cost limits. Persisted graph state is not automatically model input.
- Normalize tool outputs before they enter model context. Filter, paginate, aggregate, or extract first; offload large raw outputs as Artifacts. Return a useful bounded preview, coverage, and a scoped retrieval reference. Do not silently truncate or send the entire oversized output to another model.
- Use task-local execution messages. Hand off compact typed results and references, rather than concatenating all transcripts into one global prompt. Separate context does not require a separate autonomous agent per task.
- Track a versioned working brief: pinned user constraints, confirmed facts, hypotheses, uncertainties, completed work, and open questions. Prefer one authoritative runtime working state in checkpoints; if Job stores a UI summary, identify it as a projection and reference the authoritative revision.
- Explicitly separate transient failure retries, semantic correction, and research replanning. Enforce deadlines, maximum steps/tool calls, concurrency limits, and spend limits in code.
- Pause/resume requires a durable LangGraph checkpointer and a stable workflow identifier. A node may run again on resume; checkpoints do not guarantee exactly-once external effects. Define how graph progress and application records reconcile.
- Treat retrieved documents/tool text as untrusted data, and enforce tool permissions in code. Credentials never belong in model prompts, transcripts, or tracing payloads.

### Candidate entity fields/contracts

- Task: taskType and dependency/input references; candidate common result envelope includes summary, structuredData, evidenceRefs, artifactRefs, uncertainties, and coverage. Task-specific data is validated, results remain bounded, and a result-format version is added only if incompatible formats must coexist.
- Artifact: owner/access scope, immutable version/content identity, storage reference, source location, producing task/call if applicable, and derivation references. Preserve provenance when reprocessing or replacing sources.
- ModelCall: stage, actual model identity, selected context snapshot/reference, prompt/tool/context-policy versions, source/state revisions, token budget, usage, outcome, and optional trace identifier. Store exact inputs only where privacy policy permits; mark redactions rather than claiming fully reproducible context. Unknown usage after a timeout is not zero.
- Job: pinned constraints and a reference/version for working state, with explicit ownership between application records and graph checkpoints.
- Report: distinguish inputs consulted, claim-level evidence references, and output deliverables. Do not union every Task artifact into the final report. A report can exist without a PDF or other generated file.
- IntegrationTool: descriptions, input/output contracts, read/write classification, and supported filtering/pagination. Our adapter enforces context limits even when upstream MCP tools cannot be changed.
- ProposedAction: exact approved payload/version/hash, decision identity/time, and execution attempts/outcomes, including uncertain outcomes and reconciliation. Changing meaningful payload parameters requires fresh approval.

### Storage boundaries

- DynamoDB holds bounded operational records and references. A 400 KB item limit is not a target; do not embed unlimited transcripts, result rows, or artifact collections into Job or Task.
- Object storage holds files and large outputs. Retain authorization checks when following Artifact references.
- LangGraph checkpoint persistence is a separate explicit implementation choice; saving a Task in DynamoDB does not itself checkpoint the graph. Checkpoints should avoid copying whole raw datasets.
- Context engineering policies and operational idempotency apply regardless of database selection. DynamoDB Streams/Lambda/SQS delivery is at least once; duplicate delivery must resolve to existing work rather than blindly creating duplicate Jobs or actions.

## Learning scenario

Use synthetic September/October payment spreadsheets, credit card statement PDFs, and simulated Gmail results. Request: compare spending, identify subscription increases and missing refunds, and verify bills, citing evidence. Exclude confirmed own-account transfers and avoid counting card purchases and bill settlements twice.

Apply accounting rules before SQL totals. SQL calculates exact values; models help interpret and investigate ambiguous records. Monthly totals alone cannot establish subscriptions, expected refunds, or settled bills. Preserve date windows, source versions, missing coverage, and unresolved matches.

Start with a hybrid workflow: scope/clarify -> plan -> validate -> bounded execution -> prepare task results -> check evidence gaps -> targeted follow-up or report. Planner is a model-calling graph node, not necessarily a separate service. LangGraph coordinates execution; code defines permissions, budgets, stopping rules, and context policy.

Evaluate baseline behavior on representative synthetic cases, then deliberately force compression. Check arithmetic, citations, coverage, retained exclusions, unresolved uncertainty, original-goal preservation, source freshness, and ability to recover evidence omitted from active context. Record token usage, latency, and cost. LangSmith later observes selected/redacted data; it is not the system of record or the context manager.

## Next design discussion

Define a context contract for planner input, task input, tool response, Task.result, and report input. For each: mandatory facts, relevant/authorized sources and tools, budget and response allowance, offloading/compaction behavior, retrieval path, provenance, and failure behavior. Use one worked synthetic request to examine these handoffs before finalizing APIs or physical DynamoDB keys/indexes.

## First-party readings

Concepts and thresholds in dated blogs are examples, not immutable defaults. Map ideas to our app and check current documentation before implementation.

- LangChain, Context Engineering, 2 July 2025: https://www.langchain.com/blog/context-engineering-for-agents — write, select, compress, isolate.
- Anthropic, Effective context engineering for AI agents, 29 September 2025: https://www.anthropic.com/research/effective-context-engineering-for-ai-agents — context selection, just-in-time retrieval, compaction, notes, focused agent contexts.
- Anthropic, Writing effective tools for agents—with agents, 11 September 2025: https://www.anthropic.com/research/writing-tools-for-agents — tool clarity, bounded useful outputs, evaluation.
- LangChain, Context Management for Deep Agents, 28 January 2026: https://www.langchain.com/blog/context-management-for-deepagents — offloading, recoverable summaries, targeted compaction evaluations. Deep Agents behavior is not automatic LangGraph behavior.
- Anthropic, How we built our multi-agent research system, 13 June 2025: https://www.anthropic.com/research/multi-agent-research-system — delegation, context isolation, artifact references, cost/reliability tradeoffs.
- LangChain, Context Engineering: How Deep Agents Remember, 14 October 2026: https://www.langchain.com/blog/context-engineering-deep-agents-remember — progressive disclosure and memory patterns.
- LangGraph graph API: https://docs.langchain.com/oss/python/langgraph/graph-api
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangSmith observability concepts: https://docs.langchain.com/langsmith/observability-concepts
- LangSmith thread grouping: https://docs.langchain.com/langsmith/threads
- LangSmith model-call instrumentation: https://docs.langchain.com/langsmith/log-llm-trace
- LangGraph interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- LangChain context engineering: https://docs.langchain.com/oss/python/langchain/context-engineering
- LangSmith trace privacy: https://docs.langchain.com/langsmith/mask-inputs-outputs
- DynamoDB limits: https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Constraints.html
