# Agent components research: what makes an agent effective

Verified 4 November 2026. Primary sources are Anthropic engineering posts, LangChain documentation, and the CoALA paper. This is a design reference, not an implementation record. It maps each component to the ContextForge entity model so that field definitions can cite a requirement.

## 1. Components that the sources agree on

| Component | What the sources say | ContextForge mapping |
|---|---|---|
| Context builder (select, compress) | Find the smallest set of high-signal tokens; recall degrades as context grows ("context rot"). Curate before every call, not once. | Context Builder before every ModelCall; ModelCall records the context reference and instruction version |
| Tool interface quality | The agent-computer interface deserves as much design as a human interface. Few, well-scoped, non-overlapping tools; namespaced names; natural-language identifiers; pagination and truncation defaults; actionable error text; `response_format` concise/detailed | IntegrationTool needs description, namespace, read/write classification, pagination/filtering support, and a schema hash. Tool retrieval loads only selected schemas |
| Structured note-taking outside the context window | Plans, progress, and decisions persist in files or state, not in the transcript. Anthropic's harness uses a JSON feature list (models overwrite JSON less readily than Markdown) and a progress log, and says compaction alone is not enough | The working brief is a typed field group in the LangGraph state, checkpointed per request. It is structured data, not free Markdown |
| Compaction with recall-first rules | Keep decisions, unresolved problems, constraints, exact results, and next steps; drop old tool outputs first | Already in section 6 of the plan. Task.result and the working brief are the compaction-survivable records |
| Isolation through sub-tasks | Sub-agents work in clean contexts and return condensed summaries of roughly 1,000 to 2,000 tokens; large outputs go to a store and a reference comes back | Task branches with the bounded Task.result envelope; large results in Artifacts with references |
| Resume from checkpoints | Stateful errors compound; resume from the last good checkpoint instead of restarting; retry transient failures deterministically | DynamoDBSaver checkpoints; execution status on the request Message is a projection, not the scheduler |
| Human oversight gates | Classify tools by risk; require approval for destructive operations; bound iterations and sub-agent counts | IntegrationTool permission classification; ProposedAction for writes; per-request step and spend limits |
| Tracing plus evaluation loop | Trace decision patterns; start with about 20 representative queries; use an LLM judge with a rubric plus end-state checks; human review stays essential; promote trusted evaluators into runtime checks | LangSmith plus the scenario list in section 14 of the plan |
| Citation as its own step | Anthropic's research system runs a separate citation pass that attaches claims to sources after synthesis | Report evidence attachment is a distinct stage value on ModelCall |
| Simplicity | Workflows with predefined paths before free agents; add complexity only when measured outcomes improve | The hybrid flow is a workflow with bounded agent loops |

## 2. Memory taxonomy, confirmed

Scope (short-term versus long-term) and content (semantic, episodic, procedural) are separate axes. LangGraph persists short-term state with a checkpointer and long-term state with a store addressed by namespace and key.

CoALA adds two points that matter for us. Working memory is a data structure that persists across model calls and connects everything else; it is not only the context window. And procedural memory must be seeded by the developer: writing new procedures at runtime is "significantly riskier than writing to episodic or semantic memory" because it can introduce bugs or let the agent subvert its designers' intentions.

LangChain's profile-versus-collection distinction: a profile is one JSON document that is easy to update but error-prone as it grows; a collection is many small documents with better recall but harder updates. Write memory on the hot path when the user must see it saved immediately; write in the background when latency matters.

## 3. How ContextForge handles each memory kind

| Memory kind | Where it lives | Entity impact |
|---|---|---|
| Working memory / scratchpad / working brief | LangGraph graph state for the request thread, checkpointed. Typed fields: goal, constraints, plan, completed tasks, supported facts, open questions, next action | No entity. Defined in the graph state schema. Selected fields enter each ModelCall |
| Chat continuity (short-term, cross-request) | Prior Messages, Task.results and Reports in the same Chat, selected explicitly for a new request | Chat, Message, Report. A versioned Chat summary is a later field or Artifact, only if needed |
| Procedural (developer rules) | One versioned `agent-memory.md`, loaded by the Context Builder, not writable by the model | No entity. ModelCall records the version hash |
| Semantic (user facts, preferences) | Initially a small preferences profile on User (currency, timezone). Later a user-scoped Agent Memory collection when facts grow | User now; AgentMemoryRecord later |
| Episodic (verified past reconciliations) | Later: curated records created from completed requests, with date, outcome and evidence references, retrieved as examples only | AgentMemoryRecord later, kind = episodic |
| Learned procedures (agent edits its own rules) | Not permitted at runtime. Proposals can be logged for developer review | None |

Rules that follow from the research:

- Memory writes are explicit first ("remember this", corrections, reviewed imports). Automatic promotion of task results comes later and only against a no-episode baseline.
- Dated evidence stays in Task.result and Reports. "This bill is unpaid" is never promoted to a timeless fact.
- Retrieval checks owner, active status and expiry in application code. A retrieved permission claim never grants access.
- Credentials never enter any memory tier.

## 4. Gaps this research exposes in the current design

1. The working brief needs a defined structure now, not later. Compaction and resume both depend on it, and the harness article shows free-form notes get overwritten.
2. IntegrationTool needs fields for pagination/filter support and a response-detail option, because token-efficient tool output is a first-order effectiveness lever.
3. ModelCall stage values should include a citation/evidence-attachment stage.
4. The evaluation plan should name the first 20 representative requests early; large effects show at that size.
5. User gains an application record so that user-scoped preferences and later memory have an owner.

## Sources

- [Anthropic: Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents)
- [Anthropic: Building effective agents](https://www.anthropic.com/research/building-effective-agents)
- [Anthropic: Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents)
- [Anthropic: How we built our multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system)
- [CoALA: Cognitive Architectures for Language Agents](https://arxiv.org/html/2309.02427v3)
- [LangChain: Memory concepts](https://docs.langchain.com/oss/python/concepts/memory)
- [LangChain: Context engineering for agents](https://www.langchain.com/blog/context-engineering-for-agents)
- Secondary (vendor and practitioner, lower confidence): [Arthur: Evaluating agents in production](https://www.arthur.ai/column/evaluating-ai-agents-in-production), [AWS: Evaluating AI agents at Amazon](https://aws.amazon.com/blogs/machine-learning/evaluating-ai-agents-real-world-lessons-from-building-agentic-systems-at-amazon/), [Galileo: Agent observability](https://galileo.ai/blog/ai-agent-observability), [Patronus: Agentic memory](https://www.patronus.ai/ai-agent-development/agentic-memory)
