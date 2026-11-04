# ContextForge

Language for a private research assistant combining uploaded sources, connected services, and evidence-backed answers.

## Language

**User**:
An authenticated person identified by the identity provider, owning Chats, Artifacts, KnowledgeBases, IntegrationConnections and a small preferences profile.
_Avoid_: Client-supplied userId, learned facts or episodes stored on the User record

**Chat**:
A grouping of related user requests and follow-up interactions.
_Avoid_: Conversation, when naming this entity

**Message**:
A user-visible entry in a Chat, such as a request, answer, clarification, or proposed-action presentation. The initial user request Message also represents the work being executed for that request.
_Avoid_: Job as a separate entity, TranscriptMessage as a synonym

**Request**:
A new unit of user-requested work represented by its initial user Message. Clarification replies and approval decisions continue an existing Request.
_Avoid_: Every user Message as a new Request

**Task**:
A bounded unit of work within a Request, with a goal, execution status, and result.

**Task Result**:
The usable outcome of a Task, including supported observations, evidence references, coverage, and uncertainty.
_Avoid_: Finding as a separate initial entity

**TranscriptMessage**:
An ordered internal exchange during execution, including a tool request or response, held in LangGraph checkpoint state and LangSmith traces. Not a stored application entity.
_Avoid_: User-visible Message, general audit event, task status event, a separate transcript table

**Report**:
A Message of kind report: the answer to a request, with claim-level evidence, inputs consulted, uncertainties and coverage in its report object. One per completed request.
_Avoid_: PDF as a synonym for the Report itself, a separate versioned Report table

**ModelCall**:
One invocation of a model, including its input context, outcome, and usage information.
_Avoid_: TokenUsage as a separate entity for the same invocation

**ProposedAction**:
A Message of kind proposed_action: a concrete external operation submitted for a user's decision, carrying its approval and execution history in its action object.
_Avoid_: Clarification question, executed action as a synonym for approved action, a separate ProposedAction table

**KnowledgeBase**:
Not an entity. Searchable sources are Artifacts with indexStatus INDEXED; embedding settings are application config.
_Avoid_: A separate collection record before a named-collection requirement exists

**Artifact**:
A stored source or produced object, such as a file, dataset, large result, or generated deliverable.

**Integration**:
A supported external provider and its available connection capabilities.
_Avoid_: User account connection

**IntegrationConnection**:
A user's authorized connection to a particular account at an external provider.
_Avoid_: Integration, plaintext connector token

**IntegrationTool**:
An operation offered through an Integration, with an input/output contract and permission classification.

**Working Brief**:
The current research goal, constraints, supported facts, uncertainties, and remaining questions for a Request.
_Avoid_: Full transcript, private model reasoning

**Scratchpad**:
The Request-scoped working brief and progress notes used during ongoing research.
_Avoid_: Global mutable notebook shared by all Chats, cross-chat memory as a synonym

**Agent Memory**:
The default agent's reusable instructions and, later, selected knowledge or experiences across Chats; initially a single active versioned procedural document.
_Avoid_: MemoryEntry as the initial entity name, per-Request scratchpad, all historical transcripts

**User Memory**:
Selected reusable information belonging to one user and available across that user's Chats with the default agent.
_Avoid_: Shared private facts across all users

**Agent Instructions**:
The controlled, versioned procedures and behavioral defaults of the default agent.
_Avoid_: Arbitrary uploaded document instructions as privileged policy

**Context Snapshot**:
The particular instructions, evidence, messages, and tools presented to one ModelCall.
_Avoid_: Entire stored Request state, full transcript

**Evidence Reference**:
A link to an identifiable source location supporting an observation or claim.
_Avoid_: An unrelated file attachment as proof
