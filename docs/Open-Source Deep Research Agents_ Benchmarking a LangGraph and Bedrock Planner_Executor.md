# Open-Source Deep Research Agents to Benchmark a LangGraph + Bedrock Planner/Executor Against

The single best repo to study for your stack is LangChain's **open_deep_research**. It is Python + LangGraph, it uses a scope → supervisor-with-parallel-sub-researchers → one-shot writer design, and it has a published Deep Research Bench eval harness. Study it together with **google-gemini/gemini-fullstack-langgraph-quickstart**, the cleanest LangGraph-backend + React-frontend streaming reference, and **bytedance/deer-flow**, the most production-complete LangGraph + Next.js system. A two-agent planner/executor loop on a weak planner model is missing at least five things these repos treat as table stakes: a clarification/brief step, parallel sub-agents with isolated context, compression of findings, explicit stopping budgets, and evals.

## TL;DR

- **Study these, in order:** (1) langchain-ai/open_deep_research (12.7k★, MIT, archived Aug 21, 2026 but still the clearest reference architecture), (2) google-gemini/gemini-fullstack-langgraph-quickstart (18.4k★, Apache-2.0, LangGraph + React/Vite with streaming), (3) bytedance/deer-flow (82.8k★, MIT, LangGraph + Next.js 16/React 19 over SSE, with persistence and tracing), (4) assafelovic/gpt-researcher (30.5k★, Apache-2.0, FAST/SMART/STRATEGIC model tiers with native Bedrock support, WebSocket streaming, Next.js UI). Use langchain-ai/deepagents (31.1k★) as the actively maintained successor to open_deep_research.
- **Biggest architectural gap:** strong repos don't run "planner → one executor." They run *clarify → research brief → supervisor that fans out 1–N sub-researchers with isolated context → each sub-researcher compresses its findings → supervisor reflects and stops on budget → one writer call with citations*. Anthropic reports that an Opus-lead/Sonnet-subagent multi-agent system beat single-agent Opus by 90.2% on its internal eval, and that token usage alone explains 80% of performance variance on BrowseComp.\[1\]
- **Model choice matters more than you think:** the planner/supervisor needs reliable tool calling and structured output. GPT-OSS is a strong reasoner but scores poorly on third-party agentic rankings, and LangChain's own local-deep-researcher warns that gpt-oss doesn't support JSON mode in Ollama.\[2\]\[3\] Move the planner/supervisor and the final writer to a Claude Sonnet/Opus-class model on Bedrock first. Keep a cheap model (Haiku-class) for search-result summarization.

## Key Findings

### Ranked shortlist for your stack (Python/LangGraph backend, React frontend, Bedrock)

| Rank | Repo | Why study it | Stack match |
|---|---|---|---|
| 1 | langchain-ai/open_deep_research | Canonical supervisor/sub-agent LangGraph design. Explicit config knobs for iterations, concurrency, and tool-call budgets. Per-role models. Token-limit recovery. Deep Research Bench harness.\[4\] | Python + LangGraph (exact match). No custom React UI (uses LangGraph Studio / Open Agent Platform).\[5\] |
| 2 | google-gemini/gemini-fullstack-langgraph-quickstart | Smallest end-to-end example of LangGraph backend → React frontend with live progress of intermediate steps, a reflection loop, and citations. Production notes on Redis pub-sub + Postgres.\[6\] | Python LangGraph/FastAPI + React/Vite (exact match). Gemini-only by default. |
| 3 | bytedance/deer-flow (2.0; 1.x branch for classic deep research) | Production concerns your project will hit: SSE run streaming with Last-Event-ID replay, Postgres/SQLite checkpointing, Redis stream bridge, LangSmith/Langfuse tracing, memory, sub-agent caps and timeouts. The 1.x branch has a classic coordinator → planner → human_feedback → research_team → reporter graph.\[7\] | Python LangGraph/LangChain + Next.js 16/React 19 + LangGraph SDK (strong match). Much bigger than you need.\[8\] |
| 4 | assafelovic/gpt-researcher | Oldest and most battle-tested. Three-tier model routing (FAST/SMART/STRATEGIC) with documented Bedrock config. Many retrievers/scrapers. MCP. LangGraph multi-agent variant with a reviewer/reviser loop. WebSocket progress events. Next.js UI. evals/ folder. | Python (core is custom asyncio; multi_agents/ is LangGraph) + Next.js. |
| 5 | langchain-ai/deepagents (examples/deep_research) + deep-agents-ui | Where LangChain moved its effort. Harness with planning (todos), virtual filesystem for offloading, sub-agents, summarization middleware, and a React UI.\[9\] | Python LangGraph + React/Next UI. |

Honorable mentions: **stanford-oval/storm** (~31.6k★, MIT, dspy rather than LangGraph) for its outline-first, multi-perspective question-asking.\[10\] **langchain-ai/local-deep-researcher** (8.5k★, MIT) for the simplest IterDRAG-style LangGraph loop.\[11\] **dzhng/deep-research** (~19.1k★, TypeScript, under 500 lines) and **jina-ai/node-DeepResearch** (5.2k★, TypeScript) for minimal breadth/depth loops.\[12\]\[13\] **huggingface/smolagents examples/open_deep_research** reached 55% pass@1 on the GAIA validation set vs 67% (67.36% per Hugging Face's Agents Course) for OpenAI's Deep Research, using code-acting agents.

## Details: Per-Repo Breakdown

### 1. langchain-ai/open_deep_research

**1. Metadata.** https://github.com/langchain-ai/open_deep_research. About 12.7k stars, 1.9k forks, MIT. **Archived (read-only) on Aug 21, 2026.**\[14\] Before that, recent commits were mostly dependency bumps, and the architecture has been stable since the July 2025 supervisor rewrite.\[15\] LangChain now positions Deep Agents as its primary harness, and downstream projects (e.g. OPEA) have migrated from open_deep_research to deepagents.\[16\] Treat it as a frozen reference implementation, not a dependency.

**2. Stack.** Python, LangGraph `StateGraph` with subgraphs, `init_chat_model` for any provider,\[17\] served with `langgraph dev` (LangGraph Server API on :2024). UI is LangGraph Studio or Open Agent Platform; there is no bundled React app.\[14\] Streaming comes from the LangGraph Server's SSE run-stream endpoints. Persistence is whatever the LangGraph server provides (in-memory locally, Postgres/Redis on the platform).

**3. Architecture.**
- **Main graph:** `clarify_with_user` → `write_research_brief` → `research_supervisor` (subgraph) → `final_report_generation`.\[17\]
- **Scope phase:** an optional clarification question (`allow_clarification`, default True).\[18\] Then the chat is compressed into a **research brief**, which LangChain calls the "north star" the supervisor measures against.\[19\]
- **Supervisor subgraph** (`supervisor`, `supervisor_tools`): tools are `ConductResearch`, `ResearchComplete` and `think_tool`. Each `ConductResearch` call spawns a researcher subgraph with an isolated context. Several calls in one turn run in parallel, up to `max_concurrent_research_units` (default 5).\[20\] Extra calls get back an error message instead of running.\[17\]
- **Researcher subgraph** (`researcher`, `researcher_tools`, `compress_research`): a ReAct tool loop capped by `max_react_tool_calls` (code default 10). Then a `compress_research` LLM call rewrites the raw tool output into a cleaned, cited answer before returning to the supervisor.\[17\]\[19\]
- **Stopping criteria:** the supervisor stops when `research_iterations > max_researcher_iterations` (code default 6), when it makes no tool calls, or when it calls `ResearchComplete`.\[17\]\[18\] Note that the README/PyPI text says defaults of 3 and 5.\[21\] The code (6 and 10) is authoritative, and Exa's write-up on open-source deep research agents confirms that "by default, Open Deep Research limits the supervisor to 6 research rounds and each researcher to 10 tool calls."
- **Report:** a single one-shot writer call over the brief plus all compressed findings. LangChain abandoned parallel section-writing because the reports came out "disjoint".\[19\]
- **Human-in-the-loop:** only the clarification step in the current version. The legacy `src/legacy/graph.py` has plan-approval HITL.\[14\]
- **Error handling:**
  - Tools are wrapped in `execute_tool_safely`, which returns error strings instead of raising.\[17\]
  - `compress_research` retries up to 3 times, trimming messages back to the last AI message on token-limit errors.\[17\]
  - `final_report_generation` retries up to 3 times, truncating findings to 4× the model's token limit in characters and then cutting another 10% per retry.\[17\]
  - Caveat: the supervisor's exception handler uses `is_token_limit_exceeded(...) or True`, so any error during parallel research silently ends the research phase.\[17\]

**4. Model strategy.** Four role-specific models in `configuration.py`:
- `summarization_model` (default `openai:gpt-4.1-mini`, summarizes raw search pages, 50,000-char page cap)\[18\]
- `research_model` (`openai:gpt-4.1`)\[18\]
- `compression_model` (`openai:gpt-4.1`)\[18\]
- `final_report_model` (`openai:gpt-4.1`)\[18\]

Any `init_chat_model` provider works, Bedrock included. Models must support tool calling and structured output.\[14\] For Bedrock you will also need to add your model IDs to the token-limit map in `utils.py`; otherwise report-truncation recovery fails with a "update the model map" error.\[17\]

**5. Tooling.** Tavily by default. Native Anthropic and OpenAI web search are options, as is "none". Full MCP support (`mcp_config` with url/tools/auth).\[18\]\[19\] Search results are summarized by the cheap model before reaching the researcher.

**6. Streaming/UI.** LangGraph Server streaming (values/updates/messages); Studio shows the node-by-node graph. To build your own React UI, use `@langchain/langgraph-sdk`'s `useStream` against the same server, or reuse the gemini-quickstart frontend.

**7. Evaluation.** Built for Deep Research Bench: 100 PhD-level tasks (50 English, 50 Chinese) across 22 fields, scored on RACE with a Gemini LLM-as-judge against expert reference reports.\[14\] `tests/run_evaluate.py` runs the LangSmith experiment and `tests/extract_langsmith_data.py` exports JSONL.\[14\] Published RACE results:

| Config | RACE | Cost |
|---|---|---|
| GPT-5 as research model | 0.4943 | not published |
| Defaults (GPT-4.1) | 0.4309 | $45.98 |
| Claude Sonnet 4 as research model | 0.4401 | $187.09 |
| Leaderboard submission (gpt-4.1-nano summarizer, 207M tokens) | 0.4344 (#6) | $87.83 |

The README warns that running across the 100 examples "can cost ~$20-$100 depending on the model selection."

**8. Read first.** `src/open_deep_research/deep_researcher.py` (719 lines, the whole graph), `configuration.py`, `prompts.py` (`lead_researcher_prompt`, `research_system_prompt`, `compress_research_system_prompt`, `final_report_generation_prompt`), `state.py`, `utils.py` (`think_tool`, token-limit helpers, search tools), `langgraph.json`, `tests/run_evaluate.py`, `src/legacy/`. The companion course repo is `langchain-ai/deep_research_from_scratch`.\[14\]

**9. Techniques to adopt.** Research brief as north star. A supervisor that decides *how many* sub-agents to spawn (scaling effort to the query). Context isolation per sub-topic. A compression step before results return to the supervisor. `think_tool` for explicit reflection. Hard caps on iterations, concurrency and tool calls. A one-shot final writer. Per-role models. Token-limit-aware retries.

### 2. google-gemini/gemini-fullstack-langgraph-quickstart

**1. Metadata.** https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart. About 18.4k stars, 3.1k forks, Apache-2.0.\[6\] Last updated June 14, 2026 per the GitHub org listing.\[22\] Low commit count (45), so it is a quickstart rather than a framework.\[6\]

**2. Stack.** `backend/` is a LangGraph/FastAPI app (`langgraph dev` on :2024). `frontend/` is React + Vite + Tailwind + Shadcn UI. In production, the backend serves the built frontend. LangGraph requires Redis (pub-sub broker for streaming real-time output from background runs) and Postgres (assistants, threads, runs, thread state, long-term memory, and a task queue with "exactly once" semantics). docker-compose is included.\[6\]

**3. Architecture.** Single agent graph in `backend/src/agent/graph.py`:
1. Generate initial queries.
2. Parallel web research per query (Gemini + Google Search grounding).
3. **Reflection & knowledge-gap analysis**, which decides "sufficient?" and emits follow-up queries.
4. Loop up to a configured maximum number of loops.
5. Finalize the answer with citations.

There is no supervisor, sub-agent context isolation, clarification step or HITL. This is essentially a well-built version of your loop.

**4. Model strategy.** Separate Gemini models for query generation, reflection and answer synthesis.\[6\] It is Gemini-only out of the box; swapping to `ChatBedrockConverse` is a small change in the node functions.

**5. Tooling.** Google Search via the Gemini API grounding tool. Citations come from grounding metadata. No MCP.

**6. Streaming/UI.** The React app connects to the LangGraph server (`apiUrl` in `frontend/src/App.tsx`: :2024 in dev, :8123 in docker)\[6\] and renders an activity timeline of intermediate steps (queries generated, sources found, reflection) while the answer streams in. This is the closest template to the UI you want.

**7. Evaluation.** None.

**8. Read first.** `backend/src/agent/graph.py`, `state.py`, `prompts.py`, `configuration.py`, `tools_and_schemas.py`, `frontend/src/App.tsx` and its components, `docker-compose.yml`, `backend/examples/cli_research.py`.

**9. Techniques to adopt.** A structured reflection node that outputs `is_sufficient` + `knowledge_gap` + `follow_up_queries`. Parallel fan-out of queries via LangGraph `Send`. A clean frontend pattern for showing intermediate events. A documented Redis + Postgres production topology.

### 3. bytedance/deer-flow (2.0 main; 1.x on `main-1.x`)

**1. Metadata.** https://github.com/bytedance/deer-flow. About 82.8k stars, 11.4k forks, MIT. 2.0 is a ground-up rewrite that shares no code with v1. It hit #1 on GitHub Trending on Feb 28, 2026.\[23\] Development is very active (517 open issues, 382 open PRs). The 1.x deep-research framework is maintained on the `main-1.x` branch.\[23\]

**2. Stack.**
- **Backend:** Python 3.12+, built on LangGraph and LangChain, packaged as `backend/packages/harness/deerflow/` (`agents/lead_agent/`, `agents/middlewares/`, `agents/memory/`, `subagents/`, `sandbox/`, `tools/`, `mcp/`, `models/`, `runtime/`, `persistence/`, `tracing/`, `guardrails/`). There is also a FastAPI gateway in `app/gateway/` behind Nginx (:2026).\[24\]
- **Frontend:** Next.js 16 App Router, React 19, Tailwind 4, Shadcn, LangGraph SDK and Vercel AI Elements.\[8\]
- **Persistence:** `database.backend` = sqlite or postgres, shared by the LangGraph checkpointer, the Store and app data.\[23\]
- **Multi-worker deployments:** require Postgres + a Redis stream bridge.\[23\]

**3. Architecture.**
- **2.0** is a single `lead_agent` (`make_lead_agent(config)`) with an ordered middleware stack: ThreadData → Uploads → Sandbox → Summarization → TodoList (plan mode) → Title → Memory → ViewImage → Clarification (last). It delegates via a `task()` tool to sub-agents (`general-purpose`, `bash`), capped at 3 sub-agents per turn with a 15-minute timeout.\[24\] There are also `subagents.max_total_per_run` caps, loop/token guards, long-term memory across sessions, manual context compaction, skills (Markdown capability modules), MCP, and sandboxed code execution.\[23\]\[25\]
- **1.x** has an explicit graph: `coordinator` → (optional `background_investigator`) → `planner` → `human_feedback` (plan approval/edit) → `research_team` → `researcher`/`coder` → back to `planner` or → `reporter`. Built via `build_graph_with_memory()` in `src/graph/builder.py`, with nodes in `src/graph/nodes.py`.\[26\]\[27\] This is the closest analogue to your planner/executor design, plus HITL plan review. (1.x node names come from secondary write-ups of `builder.py`; verify on the branch.)

**4. Model strategy.** YAML-configured multi-provider models (OpenAI, Responses API, OpenRouter, vLLM, Claude via Claude Code OAuth, Codex CLI, and more).\[24\] Optional per-model request admission for rate limits.\[28\] The README recommends Doubao-Seed-2.0-Code, DeepSeek v3.2 and Kimi 2.5.\[23\] Third-party reviewers note that the lead agent needs strong instruction-following and structured output, and that small models struggle with orchestration.\[29\]

**5. Tooling.** Tavily, Jina, Browserless and InfoQuest search/fetch (HTML → Markdown with resolved links), MCP servers, sandboxed bash/file tools, image search, report/slide generation.\[23\]\[24\]

**6. Streaming/UI.** The Gateway exposes a LangGraph-compatible API that streams run events over SSE (`POST /api/langgraph/threads/{thread_id}/runs/stream`, `Accept: text/event-stream`) with `stream_mode=["values","messages-tuple","custom"]`.\[30\] It also handles bounded `Last-Event-ID` replay and emits explicit `gap` events when a reconnect cursor has been trimmed.\[23\] The frontend uses a `useThreadStream` hook over `@langchain/langgraph-sdk`.\[31\]

**7. Evaluation/observability.** LangSmith, Langfuse and Monocle tracing.\[24\] Sister project "LLM Space" for replaying and benchmarking harness steps.\[23\] I did not find a public deep-research benchmark score.

**8. Read first.** `backend/README.md`, `backend/docs/API.md`, `backend/docs/CONFIGURATION.md`, `backend/packages/harness/deerflow/agents/lead_agent/`, `agents/middlewares/`, `subagents/executor.py`, `runtime/` (RunManager, StreamBridge), `config.example.yaml`, `frontend/src/core/threads/`. On `main-1.x`: `src/graph/builder.py`, `src/graph/nodes.py`, `src/prompts/`.

**9. Techniques to adopt.** SSE with resumable cursors. Checkpointer-backed threads. Middleware-based context management (summarization, todo list). Sub-agent caps and timeouts. Plan-approval HITL (1.x). Tracing hooks. A health endpoint plus `make doctor`-style diagnostics.

### 4. assafelovic/gpt-researcher

**1. Metadata.** https://github.com/assafelovic/gpt-researcher. About 30.5k stars, 4.2k forks,\[32\] Apache-2.0, 3,219 commits.\[33\] Release v3.7.0 (pip `gpt-researcher` 0.16.0) requires Python 3.12+ and switched the default context filter from embeddings to keyword ranking, which "matched or beat embeddings on every measure in the benchmark."\[34\] Actively maintained.

**2. Stack.** Python async core (`gpt_researcher/`), a FastAPI `backend/`, and two frontends: a lightweight static HTML/JS one and a Next.js + Tailwind one (`frontend/nextjs`). Streaming is over **WebSockets** (`stream_output()` events, handled in the frontend's `useWebSocket.ts`).\[33\]\[35\] Also includes `multi_agents/` (LangGraph), `deep_agents/` (Deep Agents variant), `mcp-server/`, `evals/`, and `terraform/`.\[33\]

**3. Architecture.**
- **Core:** a planner → executor → publisher pattern (inspired by Plan-and-Solve and RAG). It generates research questions, runs parallel crawlers/scrapers per question, filters context, and writes a cited report. It also has a recursive "deep research" mode (breadth × depth).\[33\]\[36\]
- **`multi_agents/` (LangGraph):** Chief Editor → Browser (initial research) → Editor (outline) → parallel per-section Researcher → Reviewer ⇄ Reviser loop (conditional edge accept/revise) → Writer → Publisher (PDF/DOCX/MD), with optional human feedback.\[37\]\[38\]
- **`deep_agents/` variant:** a chief editor plans sections as todos, delegates to researcher sub-agents with isolated context that write `sections/<n>-<slug>.md`, then reviews and requests revisions.\[39\]

**4. Model strategy.** Three tiers: `FAST_LLM` (summaries), `SMART_LLM` (report writing) and `STRATEGIC_LLM` (planning/reasoning), set as `provider:model`.\[40\] **Bedrock is documented**, e.g. `SMART_LLM=bedrock:anthropic.claude-...` plus `EMBEDDING=bedrock:amazon.titan-embed-text-v2:0`. Also supports Anthropic, Azure, Ollama, and more.\[41\]

**5. Tooling.** Many retrievers (Tavily, Bing, Google, DuckDuckGo, Exa, etc., plus third-party retriever plugins via entry points).\[34\] JavaScript-enabled scraping. Local-document research. MCP retriever (`RETRIEVER=tavily,mcp`). Exposes itself as an MCP server (gptr-mcp) and as a Claude skill.\[33\]

**6. Streaming/UI.** WebSocket events (logs, sub-queries, sources, report chunks). The Next.js UI shows real-time progress and findings.\[33\]

**7. Evaluation.** `evals/` directory plus a published hallucination leaderboard in its docs.\[34\]

**8. Read first.** `gpt_researcher/agent.py` (`GPTResearcher`), `gpt_researcher/skills/researcher.py` (`ResearchConductor`), `skills/writer.py`, `skills/browser.py`, `prompts.py`, `config/variables/default.py`, `multi_agents/agents/` (`chief_editor.py`, `editor.py`, `reviewer.py`, `reviser.py`), `deep_agents/README.md`, `backend/server/`, `frontend/nextjs/`.

**9. Techniques to adopt.** Explicit model tiering with Bedrock examples. Context filtering before the LLM. A Reviewer ⇄ Reviser critique loop. Typed progress events to the UI. Local-docs + web hybrid retrieval.

### 5. langchain-ai/deepagents (+ deep-agents-ui)

**1. Metadata.** https://github.com/langchain-ai/deepagents. About 31.1k stars, 4.4k forks, MIT, 4,645 commits, very active.\[42\] The research example lives in `examples/deep_research`; the older `deepagents-quickstarts` repo was archived Feb 11, 2026.\[43\]\[44\]

**2–3. Stack and architecture.** `create_deep_agent(model, tools, subagents, system_prompt)` on LangGraph.\[42\] It ships with:
- a planning todo list (`write_todos`)
- a virtual filesystem for offloading context (`write_file`/`read_file`)
- a `task` tool for sub-agents with isolated (or forked) context\[45\]
- summarization middleware that compacts history at about 85% of the context window\[46\]
- HITL middleware
- skills and memory

The research example runs: plan todos → save the request to `/research_request.md` → delegate to researcher sub-agents (in parallel only for explicit comparisons or separable topics) → consolidate citations → write `/final_report.md` → verify.\[47\]

**4–6.** Any `init_chat_model` provider; the docs example uses `anthropic:claude-sonnet-4-5`. Tavily + httpx/markdownify fetch.\[48\]\[49\] React UI via `langchain-ai/deep-agents-ui` (Next.js, shows todos and files in state).\[48\]

**9. Why it matters for you.** This is where LangChain's current context-engineering patterns live (offload, summarize, isolate, load skills on demand).\[46\] If you want to stay on LangGraph long-term, this is the maintained path.

### 6. Others worth a quick read

- **stanford-oval/storm** (~31.6k★, MIT, `knowledge-storm` on pip, dspy, LiteLLM models).\[10\]\[50\] Outline-first pipeline: perspective discovery → simulated multi-perspective expert conversations → outline → article → polish. Co-STORM (EMNLP 2024) adds a moderator plus a human-in-the-loop discourse and a concept "mind map".\[51\]\[52\] Commit activity has been sparse.\[53\] Read `knowledge_storm/storm_wiki/modules/*` and `collaborative_storm/engine.py`.
- **langchain-ai/local-deep-researcher** (8.5k★, MIT). Single loop: generate query → search → summarize → reflect on gaps → repeat N times → write a summary with sources (IterDRAG). It added gpt-oss support in August 2025, with a warning that gpt-oss models don't support JSON mode in Ollama, so tool calling is needed instead.\[2\]\[11\] Useful as a lower bound on "what a minimal loop looks like."
- **dzhng/deep-research** (~19.1k★, TypeScript) and **jina-ai/node-DeepResearch** (5.2k★, TypeScript, "keep searching… until… exceeding the token budget").\[12\]\[13\] Minimal breadth/depth and token-budget loops. Not your stack.

## Consolidated Capability Checklist (for the coding agent to score your code against)

Mark each as Present / Partial / Missing, with file:line evidence.

1. **Clarification step:** asks the user a follow-up when the query is ambiguous, and can be disabled (ODR `clarify_with_user`, DeerFlow ClarificationMiddleware).
2. **Research brief:** chat compressed into a structured brief that later stages are measured against (ODR `write_research_brief`).
3. **Plan representation:** plan stored as structured state or a todo list, persisted outside the context window (Anthropic saves the plan to memory; deepagents `write_todos`).\[1\]
4. **HITL plan approval/edit:** optional interrupt before execution (DeerFlow 1.x `human_feedback`, ODR legacy graph, GPTR multi_agents).
5. **Supervisor with dynamic fan-out:** the planner decides how many sub-researchers to run (1 for simple, 2–4 for comparisons, more for broad topics) and launches them in parallel (LangGraph `Send` / parallel tool calls).
6. **Context isolation:** each sub-researcher has its own message history and receives a complete standalone task description (objective, output format, tool guidance, boundaries).\[1\]
7. **Parallel tool calls inside sub-researchers:** multiple searches per turn.
8. **Reflection / gap analysis:** explicit `think_tool` or a reflection node with structured `is_sufficient`/`follow_up_queries` (gemini-quickstart).
9. **Stopping criteria and budgets:** max supervisor iterations, max tool calls per researcher, max concurrent units, wall-clock/token budget, and a `ResearchComplete` tool.
10. **Search-result summarization:** a cheap model condenses raw pages before the researcher sees them, with a page-length cap.
11. **Findings compression:** each sub-researcher returns a pruned, cited synthesis rather than raw tool output.\[19\]
12. **Context-window management:** token counting, summarization at a threshold, offloading to a store/filesystem, recovery on context-length errors.
13. **Citation tracking:** stable source IDs carried from search → notes → report, deduplicated, with a final numbered source list. Optional citation-verification pass (Anthropic's CitationAgent).\[1\]
14. **One-shot final writer:** a separate (strong) model writes the report from brief + findings. No parallel section writing.
15. **Per-role model configuration:** planner/supervisor, researcher, summarizer, compressor and writer each configurable, with provider-agnostic init.
16. **Tool robustness:** tool errors returned as observations rather than exceptions. Retries with backoff for throttling (Bedrock `ThrottlingException`). Structured-output retries.
17. **Checkpointing/durable execution:** resume a failed run from the last node, not from scratch (LangGraph checkpointer; DynamoDB checkpointer is an option on AWS).
18. **Streaming protocol:** typed events (node start/end, plan, sub-agent spawned, query issued, source found, token chunks, final) with resumable reconnect (Last-Event-ID) and heartbeats.
19. **Frontend:** activity timeline of intermediate steps, per-sub-agent progress, sources panel, final report with clickable citations, plan-approval UI.
20. **Observability:** tracing of every LLM/tool call with token and cost accounting (LangSmith/Langfuse/OpenTelemetry).
21. **Evals:** a small golden set (~20 queries) to start, an LLM-as-judge rubric (factual accuracy, citation accuracy, completeness, source quality, tool efficiency), and optionally Deep Research Bench RACE.\[1\]
22. **Effort scaling:** prompt heuristics so simple queries don't trigger expensive fan-out.\[1\]
23. **Source-quality heuristics:** prefer primary sources over SEO content farms.\[1\]
24. **Extensibility:** MCP tool loading, a URL-fetch/scrape tool beyond search, and optional local-doc retrieval.
25. **Security/limits:** per-user rate limits, API-key handling, prompt-injection awareness for fetched pages.

## Authoritative Write-ups (summarized)

- **Anthropic, "How we built our multi-agent research system" (June 13, 2025).**
  - Orchestrator-worker design: a LeadResearcher plans, saves the plan to Memory (contexts over 200k tokens get truncated), spawns sub-agents that search in parallel with interleaved thinking, then decides whether more research is needed. A CitationAgent attributes claims at the end.\[1\]
  - Opus 4 lead + Sonnet 4 sub-agents outperformed single-agent Opus 4 by 90.2% on their internal eval. Token usage explained 80% of BrowseComp variance, with tool-call count and model choice making up most of the rest (95% total). In Anthropic's words, "agents typically use about 4× more tokens than chat interactions, and multi-agent systems use about 15× more tokens than chats."
  - Prompting lessons: teach the orchestrator to delegate (objective, output format, tools, boundaries); embed effort-scaling rules (1 agent / 3–10 calls for simple, 2–4 sub-agents / 10–15 calls each for comparisons, 10+ for complex); start wide then narrow; 3–5 parallel sub-agents with 3+ parallel tool calls each cut research time by up to 90%.\[1\]
  - Evals: start with about 20 queries, use a single-call LLM-judge rubric, and keep human testing.\[1\]
  - Production: resume-from-checkpoint instead of restart, full tracing, rainbow deploys. Synchronous sub-agent execution is a known bottleneck.\[1\]
- **LangChain, "Open Deep Research" (July 16, 2025).**
  - Scope → Research → Write.\[19\]
  - Lessons: use multi-agent only for parallelizable research, not writing. Isolate context per sub-topic, because single agents researching A vs B vs C juggled mixed context and researched each topic less deeply. Let the supervisor tune depth. Use context engineering (brief compression, findings pruning) to avoid token bloat, context-limit failures and TPM rate limits.\[19\]
  - A companion post by Lance Martin ("bitter lesson", July 30, 2025) traces the evolution from the earlier workflow and multi-agent versions.\[14\]
- **LangChain Deep Agents posts (2025–2026).** Context-rot mitigation via offloading, summarization (at about 85% of the window), skills loaded on demand, isolated or forked sub-agents, and memory.\[46\] Recent posts cover async sub-agents (Nov 2026).\[19\]
- **Anthropic Opus 4.6 system card (Feb 2026), multi-agent BrowseComp setup.** The orchestrator has *no direct tools*, only sub-agent spawning. Sub-agents get 200k context with web search, web fetch and programmatic tool calling. The orchestrator compacts at 50k tokens with a 1M total-token limit.\[54\] This is a useful signal that "planner without tools + workers with tools" is the current frontier pattern.

## Model Choice: How Much a Weak Planner Hurts, and What to Use

- **The planner/supervisor is the highest-leverage model.** It decides decomposition, sub-agent count, task descriptions and when to stop. Anthropic's own data shows model capability multiplies token efficiency: upgrading to Claude Sonnet 4 was a bigger gain than doubling the token budget on Claude Sonnet 3.7.\[1\] The strong repos encode the same split:
  - Anthropic uses Opus lead + Sonnet workers.
  - ODR's published runs vary the research model (GPT-4.1 → GPT-5 moved RACE from 0.4309 to 0.4943), while summarization stays on a mini model.\[14\]
  - GPT Researcher puts its strongest model in `STRATEGIC_LLM` and the cheapest in `FAST_LLM`.
- **GPT-OSS as planner is a real handicap for this workload.** GPT-OSS-120B posts strong reasoning numbers (OpenAI reports 92.5% AIME 2025 and 80.1% GPQA Diamond at high effort).\[55\] Agentic and tool-use rankings are weaker:
  - BenchLM.ai ranks it #109 of 131 eligible models for agentic tool use and computer tasks (category score 27.6/100, based on a single benchmark).
  - Vals AI's Finance Agent v1.1 leaderboard puts it at 21.54% accuracy (rank 45) on 537 entry-level financial-analyst questions.
  - It depends on the harmony response format, and broken templates are a common cause of underperformance.\[55\]
  - LangChain's local-deep-researcher notes gpt-oss doesn't support JSON mode in Ollama.\[2\]
  - DeerFlow reviewers likewise note that smaller models struggle with lead-agent orchestration.\[29\]

  Expect the symptoms you would see with a weak planner: vague or duplicated sub-tasks, malformed structured output, premature or never-ending loops, and shallow coverage. These are aggregator and third-party figures, not controlled tests on your harness, so verify with your own evals.
- **Recommended assignment on Bedrock:**

  | Role | Model |
  |---|---|
  | Planner/supervisor | Claude Opus-class (or Sonnet-class if cost-constrained) |
  | Sub-researchers | Claude Sonnet-class |
  | Search-page summarizer + compression | Claude Haiku-class (or keep GPT-OSS here, where formatting demands are low) |
  | Final writer | Sonnet/Opus-class |

  Use `ChatBedrockConverse` via `init_chat_model("bedrock_converse:<model-id>")` so each role is a config string. Validate tool-calling and structured-output reliability per model before relying on them. Budget for cost: in ODR's results, the Claude Sonnet 4 research-model run cost $187.09 vs $45.98 for GPT-4.1 defaults on the same 100-task benchmark.\[14\]

## Recommendations

1. **Restructure the graph** to ODR's shape: `clarify → brief → supervisor subgraph (ConductResearch/think/ResearchComplete) → researcher subgraph (tool loop → compress) → final_report`. Keep your planner as the supervisor and turn the single executor into N parallel researcher instances via `Send`.
2. **Upgrade the supervisor and writer models first.** That is the cheapest quality win. Leave summarization on a small model.
3. **Add budgets and recovery:** iteration, tool-call and concurrency caps, Bedrock throttling retries, and token-limit truncation retries. Don't copy ODR's `or True` catch-all.
4. **Replace ad-hoc SSE** with typed events backed by LangGraph `stream_mode=["updates","messages","custom"]` and `subgraphs=True`,\[56\] plus event IDs for resume. Alternatively, put the graph behind a LangGraph-compatible API and use `useStream` in React (the gemini-quickstart and DeerFlow pattern).
5. **Use a LangGraph checkpointer for run state** (Postgres, or a DynamoDB-backed saver) rather than hand-rolled DynamoDB writes. Keep SQS only for kicking off background runs.
6. **Add a 20-query eval set with an LLM-judge rubric now.** Run Deep Research Bench later once the architecture stabilizes.

## Ready-to-Use Prompt for Your Coding Agent

```
You are auditing my local research-agent codebase against leading open-source deep-research agents.
Work in a scratch dir (e.g. ./_refs). Do not modify my code until the report is approved.

STEP 1 — Clone references (shallow):
  git clone --depth 1 https://github.com/langchain-ai/open_deep_research _refs/odr   # archived; reference only
  git clone --depth 1 https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart _refs/gemini
  git clone --depth 1 https://github.com/bytedance/deer-flow _refs/deerflow
  git clone --depth 1 --branch main-1.x https://github.com/bytedance/deer-flow _refs/deerflow-1x
  git clone --depth 1 https://github.com/assafelovic/gpt-researcher _refs/gptr
  git clone --depth 1 https://github.com/langchain-ai/deepagents _refs/deepagents

STEP 2 — Read these first:
  odr: src/open_deep_research/{deep_researcher.py,configuration.py,prompts.py,state.py,utils.py}, tests/run_evaluate.py, src/legacy/
  gemini: backend/src/agent/{graph.py,state.py,prompts.py,configuration.py,tools_and_schemas.py}, frontend/src/App.tsx + components
  deerflow: backend/README.md, backend/docs/API.md, backend/packages/harness/deerflow/{agents/lead_agent,agents/middlewares,subagents,runtime,persistence}, frontend/src/core/threads/
  deerflow-1x: src/graph/{builder.py,nodes.py}, src/prompts/
  gptr: gpt_researcher/{agent.py,skills/researcher.py,skills/writer.py,prompts.py,config/variables/default.py}, multi_agents/agents/, backend/server/
  deepagents: examples/deep_research/, libs/ (create_deep_agent, middleware: todo, filesystem, subagents, summarization)

STEP 3 — Map my code: produce a diagram (mermaid) of my LangGraph graph(s): nodes, edges, state schema,
  which Bedrock model each node uses, how SSE events are produced, what goes to DynamoDB/SQS.

STEP 4 — Side-by-side table: for each reference vs my code, compare: graph topology; plan generation/revision;
  fan-out & concurrency; context isolation; reflection; stopping criteria/budgets; clarification/brief; HITL;
  summarization/compression; token-limit handling; citations; final report synthesis; per-role models;
  tools (search, fetch/scrape, MCP); streaming protocol & event types; frontend progress UI; persistence/checkpointing;
  retries/error handling; tracing; evals.

STEP 5 — Score my code against this checklist (Present/Partial/Missing + file:line evidence):
  clarification, research brief, persisted plan, HITL plan approval, supervisor dynamic fan-out, context isolation,
  parallel tool calls, reflection/think tool, iteration/tool-call/concurrency/token budgets, search-result summarization,
  findings compression, context-window management, citation tracking & dedupe, one-shot writer, per-role model config,
  tool error handling + Bedrock throttling retries + structured-output retries, checkpoint/resume, typed resumable SSE,
  React progress UI, tracing/cost accounting, eval set + LLM-judge rubric, effort scaling, source-quality heuristics,
  MCP/fetch tools, rate limits/prompt-injection safeguards.

STEP 6 — Gap analysis report (markdown), prioritized P0/P1/P2, each item with: what the reference does (file path),
  what mine does, concrete change (files to add/modify, function signatures, prompt snippets adapted from references),
  effort estimate, and risk. Cover: structure, orchestration, prompts, model assignment (assume Bedrock:
  Opus/Sonnet planner, Sonnet researchers, Haiku summarizer; flag where GPT-OSS is likely failing — tool-call/JSON
  formatting), tooling, streaming, persistence (LangGraph checkpointer vs my DynamoDB code; SQS role), evaluation.

STEP 7 — Propose a minimal migration plan in 3 PR-sized steps, then wait for my approval.
Constraints: keep Python + LangGraph + Bedrock; no OpenAI/Gemini keys; licenses are MIT/Apache-2.0 — note attribution if copying code.
```

## Caveats

- Star counts are point-in-time readings from GitHub pages fetched for this report (Oct–Nov 2026) and move quickly. DeerFlow's count in particular has grown fast since its 2.0 launch. Third-party counts differ slightly.
- open_deep_research is archived. Its benchmark numbers come from 2025 models, and the Deep Research Bench leaderboard space has been reported unreachable at times.\[15\] Use it for architecture, not as a living dependency.
- DeerFlow 1.x node names come from secondary write-ups of `src/graph/builder.py`, and DeerFlow 2.0 file paths have changed across versions (older `backend/src/...` vs current `backend/packages/harness/deerflow/...`). Let the coding agent confirm them in the clone.
- GPT-OSS agentic-quality figures come from aggregators and vendor model cards, not controlled tests on your harness. Anthropic's 90.2% and 80%-variance figures are internal evals by a model vendor. Treat both as directional and validate with your own eval set.

## Sources

1. [How we built our multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system)
2. [local-deep-researcher/README.md at main · langchain-ai/local-deep-researcher · GitHub](https://github.com/langchain-ai/local-deep-researcher/blob/main/README.md)
3. [GPT-OSS 120B Benchmarks & Speed (November 2026)](https://benchlm.ai/models/gpt-oss-120b)
4. [Deep Research Agent Workflow](https://deepwiki.com/langchain-ai/open_deep_research/3.1-langgraph-implementation)
5. [open\_deep\_research/langgraph.json at main · langchain-ai/open\_deep\_research · GitHub](https://github.com/langchain-ai/open_deep_research/blob/main/langgraph.json)
6. [GitHub - google-gemini/gemini-fullstack-langgraph-quickstart: Get started with building Fullstack Agents using Gemini 2.5 and LangGraph](https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart)
7. [Have you considered using LangGraph's supervisor pattern for multi-agent coordination? · Issue #270 · bytedance/deer-flow · GitHub](https://github.com/bytedance/deer-flow/issues/270)
8. [deer-flow/frontend/README.md at main · bytedance/deer-flow · GitHub](https://github.com/bytedance/deer-flow/blob/main/frontend/README.md)
9. [LangChain Deep Agents Alternatives: 5 Options Compared for 2026](https://www.truefoundry.com/blog/langchain-deep-agents-alternatives)
10. [Pull requests · stanford-oval/storm · GitHub](https://github.com/stanford-oval/storm/pulls)
11. [local-deep-researcher/pyproject.toml at main · langchain-ai/local-deep-researcher · GitHub](https://github.com/langchain-ai/local-deep-researcher/blob/main/pyproject.toml)
12. [GitHub - jina-ai/node-DeepResearch: Keep searching, reading webpages, reasoning until it finds the answer (or exceeding the token budget) · GitHub](https://github.com/jina-ai/node-DeepResearch)
13. [Deep Research (dzhng)](https://rywalker.com/research/deep-research)
14. [GitHub - langchain-ai/open\_deep\_research](https://github.com/langchain-ai/open_deep_research)
15. [Open Deep Research (LangChain)](https://rywalker.com/research/open-deep-research)
16. [github.com](https://github.com/opea-project/GenAIExamples/pull/2334)
17. [open\_deep\_research/src/open\_deep\_research/deep\_researcher.py at main · langchain-ai/open\_deep\_research](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/deep_researcher.py)
18. [open\_deep\_research/src/open\_deep\_research/configuration.py at main · langchain-ai/open\_deep\_research](https://github.com/langchain-ai/open_deep_research/blob/main/src/open_deep_research/configuration.py)
19. [Open Deep Research](https://www.langchain.com/blog/open-deep-research)
20. [Building Enterprise Deep Research Agents with LangChain’s Open Deep Research](https://medium.com/@tuhinsharma121/building-enterprise-deep-research-agents-with-langchains-open-deep-research-63e7cdb80a58)
21. [open-deep-research · PyPI](https://pypi.org/project/open-deep-research/)
22. [google-gemini repositories · GitHub](https://github.com/orgs/google-gemini/repositories)
23. [deer-flow/README.md at main · bytedance/deer-flow · GitHub](https://github.com/bytedance/deer-flow/blob/main/README.md)
24. [deer-flow/backend/README.md at main · bytedance/deer-flow](https://github.com/bytedance/deer-flow/blob/main/backend/README.md)
25. [GitHub - CornellHalifax/dsh-u-20261026-bytedance\_\_deer-flow: DSH unrestricted WF validation 20261026; source bytedance/deer-flow · GitHub](https://github.com/CornellHalifax/dsh-u-20261026-bytedance__deer-flow)
26. [Custom Deep Research & Agentic Research](https://paul-bruffett.medium.com/custom-deep-research-agentic-research-f4f516509b0b)
27. [Multi-Agent Workflow Orchestration](https://deepwiki.com/bytedance/deer-flow/2.1-multi-agent-workflow)
28. [deer-flow/backend/docs/CONFIGURATION.md at main · bytedance/deer-flow · GitHub](https://github.com/bytedance/deer-flow/blob/main/backend/docs/CONFIGURATION.md)
29. [DeerFlow 2.0: What It Is, How It Works, and Why Developers Should Pay Attention - DEV Community](https://dev.to/arshtechpro/deerflow-20-what-it-is-how-it-works-and-why-developers-should-pay-attention-3ip3)
30. [deer-flow/backend/docs/API.md at main · bytedance/deer-flow · GitHub](https://github.com/bytedance/deer-flow/blob/main/backend/docs/API.md)
31. [idealiu555/deer-flow](https://deepwiki.com/idealiu555/deer-flow)
32. [gpt-researcher - AI Agents on GitHub (30.5k★)](https://skillsllm.com/skill/gpt-researcher)
33. [GitHub - assafelovic/gpt-researcher: An autonomous agent that conducts deep research on any data using any LLM providers](https://github.com/assafelovic/gpt-researcher)
34. [assafelovic/gpt-researcher v3.7.0 on GitHub](https://newreleases.io/project/github/assafelovic/gpt-researcher/release/v3.7.0)
35. [gpt-researcher - Agent Skill by assafelovic/gpt-researcher](https://agentskills.so/skills/assafelovic-gpt-researcher-gpt-researcher)
36. [assafelovic/gpt-researcher](https://deepwiki.com/assafelovic/gpt-researcher/1-overview)
37. [gpt-researcher/docs/blog/2024-05-19-gptr-langgraph/index.md at master · assafelovic/gpt-researcher · GitHub](https://github.com/assafelovic/gpt-researcher/blob/master/docs/blog/2024-05-19-gptr-langgraph/index.md)
38. [LangGraph](https://docs.gptr.dev/docs/gpt-researcher/multi_agents/langgraph)
39. [gpt-researcher/deep\_agents/README.md at main · assafelovic/gpt-researcher · GitHub](https://github.com/assafelovic/gpt-researcher/blob/main/deep_agents/README.md)
40. [FEATURE REQUEST: Allow user selection of models at query time · Issue #1289 · assafelovic/gpt-researcher · GitHub](https://github.com/assafelovic/gpt-researcher/issues/1289)
41. [Configure LLM](https://docs.gptr.dev/docs/gpt-researcher/llms)
42. [GitHub - langchain-ai/deepagents: The batteries-included agent harness.](https://github.com/langchain-ai/deepagents)
43. [GitHub - langchain-ai/deepagents-quickstarts: REPO MOVED · GitHub](https://github.com/langchain-ai/deepagents-quickstarts)
44. [deepagents/examples at main · langchain-ai/deepagents · GitHub](https://github.com/langchain-ai/deepagents/tree/main/examples)
45. [Organizing Context in a Multi-Agent Harness](https://www.langchain.com/blog/organizing-context-in-a-multi-agent-harness)
46. [Context Engineering with Deep Agents: How To Remember Without Keeping Everything In Context](https://www.langchain.com/blog/context-engineering-deep-agents-remember)
47. [Deep Research Agent](https://deepwiki.com/langchain-ai/deepagents-quickstarts/3-deep-research-agent)
48. [deepagents/examples/deep\_research at main · langchain-ai/deepagents · GitHub](https://github.com/langchain-ai/deepagents/tree/main/examples/deep_research)
49. [Build a deep research agent - Docs by LangChain](https://docs.langchain.com/oss/python/deepagents/deep-research)
50. [Releases · stanford-oval/storm · GitHub](https://github.com/stanford-oval/storm/releases)
51. [GitHub - stanford-oval/storm: An LLM-powered knowledge curation system that researches a topic and generates a full-length report with citations. · GitHub](https://github.com/stanford-oval/storm)
52. [Stanford STORM Explained: AI That Writes and Curates Smarter](https://medium.com/predict/stanford-storm-explained-ai-that-writes-and-curates-smarter-ff39c746e290)
53. [STORM by Stanford 2026: Free Wikipedia-Style AI Reports — AIVario](https://aivario.com/tools/storm-stanford)
54. [System Card:​ Claude Opus 4.6 February 2026 anthropic.com](https://www-cdn.anthropic.com/0dd865075ad3132672ee0ab40b05a53f14cf5288.pdf)
55. [GPT-OSS-120B Benchmarks: Results and How to Test](https://www.cognativ.com/blog/gpt-oss-120b-benchmark/)
56. [Streaming API - Docs by LangChain](https://docs.langchain.com/langsmith/streaming)
