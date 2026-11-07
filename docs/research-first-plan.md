# Current milestone: a dependable public-web research agent

Decision recorded November 7, 2026, from Mayur's instructions: prioritize deep public-web research with clear points, usable citations, a report comparable in structure and depth to the Claude research documents in Box, and at most 50 web-tool calls. PDF ingestion, uploads, private MCP integrations and write tools follow this milestone. No new domain entity is introduced by this decision.

## Definition of the product

A user asks a complex question in chat. The engine develops a research plan, searches and reads public sources, follows useful leads, checks conflicting evidence, and produces a coherent report. Progress and sources remain inspectable in the UI. A follow-up can refine the question using the previous report's facts, conclusions and gaps.

The target is **source-supported research with no observed unsupported material claims in the evaluated output**. No system should promise that citations or an automated judge guarantee an absence of hallucinations.

Style references reviewed through the Box plugin:

- [AI Cost Engineering Industry Research Report](https://account.box.com/notes/2572723427786): summary, company cases, comparisons, practical patterns and limitations.
- [SDE-2 Global Market Research and Prep Strategy](https://account.box.com/file/2505451854910): regional comparisons, source-type distinctions, tailored synthesis and explicit missing-input notes.
- [AI Agent System Design — Deep Research](https://account.box.com/file/2562672069321): worked examples, traceable reasoning and concrete failure/recovery scenarios. Its private-data/write-tool scenario is a design reference, outside this milestone's capabilities.

## Report contract

Each report should include a short executive summary, sections answering the requested subjects, relevant comparisons, evidence-linked conclusions, conflicting/unanswered questions, a dated methodology note and usable citations. Every material factual claim must point to supporting evidence; each significant number must retain units, geography, date, population and baseline when relevant. Vendor assertions are attributed; analytical recommendations are labeled.

A source record needs the original and resolved URL, title, publisher, retrieval time, publication/update date when established, extraction status and saved content identifier/hash. A timestamp is not invented when absent. Search snippets can guide discovery; important claims should be checked against the underlying page whenever accessible. Missing access is recorded, not silently bridged from model memory.

Reports should be generated from the retained evidence and coverage plan, rather than padded to a fixed word count. The short chat message and full deliverable serve different reading needs. The writer must preserve inline citations in narrative and tables, and cannot introduce unsupported facts after the citation stage.

## Limits and what the current code actually does

The user's limit is **50 combined `web_search`/`fetch_url` invocations per request**, shared across all tasks, retries and resumes. Failed invocations count; parallel calls reserve from the same total. Fifty is a maximum, not an instruction to spend all calls. Model calls, provider HTTP retries, money and elapsed time are separate accounting dimensions. Provider retries should be bounded and observable even when one tool invocation uses a fallback internally.

**Implemented in the research continuation, awaiting live acceptance:** `webToolCallBudget: 50` is separate from `callBudget: 50`, which limits model attempts. Each search/fetch invocation reserves a slot atomically against the request's persisted `webToolCalls` counter before execution. Both memory and DynamoDB adapters enforce this boundary; failures consume a slot. Reconstructing worker dependencies or resuming a request does not reset it. Web tasks default to 12 local tool calls; the shared request cap still applies. This does not supply durable cloud graph checkpoints or exactly-once execution.

Keep the existing $0.25 live-request cap until the user changes it. A run may stop before 50 tools because its money or deadline budget is exhausted. Do not silently lift spending limits to make a long research test pass.

## Next priorities, in order

1. **Load and verify the research build.** Restart the API, confirm `/health` reports `web-research-v3`, then use the [API runner](api-testing.md) for the brief official Python release-date question. Inspect the actual planner repair, evidence, report, model routes, tool count and estimated spend. Existing screenshots show incomplete older report paths; successful live acceptance of this build is still pending.
2. **Measure one complex research run.** Run case 001 without putting the reference answer in the agent's context. Capture its report, snapshots, trace, gaps and spend using the read-only evaluation CLI. Grade question coverage, factual support, citations, numerical reasoning, synthesis and clarity against the Box/reference material. Preserve a partial or failed run rather than rerunning until it looks good.
3. **Fix the largest observed research weakness.** Use the captured evidence to choose between search coverage, targeted follow-up queries, source reading, contradiction handling, claim extraction or report synthesis. The current prompts support coverage and adaptive searching within tasks; a separate explicit cross-task replanning loop has not been built. Measure before adding another layer.
4. **Complete frozen evidence and adjudication.** Capture replayable source/tool fixtures and review material claims for case 001. The executable scorer checks structural integrity and requires matching supplied reviews for a quality pass; it does not establish truth by phrase similarity. Expand the other nine cases into researched factual answer sets incrementally.
5. **Exercise adversarial and follow-up cases.** Include stale pages, conflicting measurements, real quotes attached to the wrong metric, inaccessible sources and injected instructions. Test whether follow-ups preserve conclusions and gaps while respecting a changed scope or date window.
6. **Harden long research runs.** Resolve duplicate resume races, SQS visibility renewal, DynamoDB graph checkpoints and stuck-request recovery. The durable tool counter is implemented, but is not proof of crash-safe orchestration.
7. **Improve the reading and inspection experience.** Evaluate narrative quality and useful comparisons before adding structured tables. Restore plans after reload and expose cumulative web use and every report/review call in Deep Dive. Source timestamps and hashes are saved; richer source presentation remains work.
8. **Release a measured research milestone.** Merge only after live acceptance and the agreed quality/reliability checks. Keep the $0.25 request cap and report where it constrains depth. PDF/uploads, private MCP, write tools and learned memory stay after this milestone.

## Validation required for the 50-call limit

Offline tests cover concurrent calls across reconstructed worker dependencies, failed invocations, persisted counters across waiting/resume, independent requests, DynamoDB conditional writes and synthesis after exhaustion. The blocked 51st invocation has no provider side effect. Model `return_result` calls are not counted as web work. Real AWS worker-crash and duplicate-delivery fault injection remains pending; the counter alone does not validate those workflows.

## Current delivery status

The reference report, selected-claim Box comparison, ten complex test specifications and 12 first-case reference checks are available. The reference research used 43 public-web query/open/find operations; it was not a ContextForge engine run.

The engine now includes the planner repair, atomic shared web budget, coverage-preserving final synthesis, source metadata and URL handling, semantic support checks for facts/summary/assessments, and a reviewed narrative writer with a cited findings fallback. Sentence-period quote recovery, complete supporting-finding checks and web reconciliation address the observed Python example failures. Both the read-only capture/grading CLI and a submit/watch API runner are implemented, with rotating backend logs. Latest checks: **226 tests pass**, Ruff lint/format pass, and strict mypy passes over 83 source files.

**Research quality remains unmeasured on this build.** This session cannot bind/connect server sockets or control Terminal; the authorized API restart remains an operator step. No successful new live run, ContextForge-versus-Box score, complete original HTTP replay corpus or nine remaining factual golden answers is claimed. Source agreement/independence, derived numerical calculations and full narrative/table parity still need evaluation. PDF, private MCP and learned memory remain deferred.
