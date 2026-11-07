# Web research benchmark: ten complex requests

[Read these cases in Box](https://app.box.com/file/2576811913690).

Created November 7, 2026. This is the first-priority product benchmark for ContextForge's **public-web research** workflow. No upload, private connector, PDF-ingestion capability or write action is required. Public papers may be read through accessible HTML; inaccessible content must be reported as a gap.

These are ten concrete test specifications, not ten completed research runs. **Case 001 has a researched reference and a comparison with the user's Box report. Cases 002–010 have prompts and grading requirements; their factual answer sets still need research and adjudication.** Do not report them as passing, frozen golden answers. The earlier mixed spreadsheet/PDF `golden-001` remains a future benchmark.

## Shared instructions

Use at most **50 combined `web_search` and `fetch_url` invocations for the entire request**, shared across tasks, retries and resumes. Failed attempts count. A batch of several tool calls counts separately; `return_result` is model control, not a web tool. Keep model-call, money and deadline limits separate. Fifty is a ceiling, not a quota. Stop when the question is supported or the budget is exhausted, and state the uncovered questions.

Research as of the request's explicit cutoff; resolve relative dates into a stated timezone. Prefer primary sources for material claims. Open the source underlying an important search result. Track publication and retrieval dates separately; do not substitute a copyright footer for a publication date. Distinguish measured results, vendor claims, hypotheses and recommendations. Resolve conflicting claims where possible, otherwise retain the disagreement.

Deliver a concise executive summary, sections covering every requested subject, useful comparison tables, an assessment tied to evidence, limitations/unanswered questions, and a usable source list. Put citations next to the material claims they support. Do not invent sources, quotes, calculations, sample sizes or missing prices. Do not guarantee perfect accuracy.

## web-research-001 — AI cost engineering across production systems

> Research how leading teams reduced the cost of LLM applications, agents and retrieval systems during 2024–2026, as of November 7, 2026. Investigate Uber, Manus, Cursor, Intercom and Anthropic; add other companies only when the evidence is strong. For each, explain the workload, architecture, optimization, measured result, baseline, date and trade-offs. Separate production expenditure from token savings, throughput, retrieval quality and vendor pricing. Compare prompt caching, context editing, tool-result filtering, model routing, specialization, plan reuse and parallel agents. Identify contradictory or insufficient evidence. Give a practical implementation sequence for a small web research product, with a transparent illustrative cost calculation and source citations.

Reference: [researched report](../../docs/research/ai-cost-engineering-report-2026-11-07.md), [Box comparison](../../docs/evals/ai-cost-box-comparison-2026-11-07.md), [adjudicated facts](../fixtures/web-research-001-reference.json).

Must preserve numeric denominators, identify publisher-reported results, and reject the false equivalences in the reference fixture. Full coverage matters more than copying the reference's wording. Do not supply the Box report or reference answer to the agent as input during a fair run.

## web-research-002 — Architecture of a reliable deep research agent

> Compare publicly documented deep research architectures from Anthropic, OpenAI, Perplexity, Google and open-source implementations as of the run date. Explain query decomposition, source discovery, page reading, evidence retention, adaptive replanning, parallelism, citation verification and final synthesis. Separate published implementation details from architectural guesses. Investigate failure modes: fabricated citations, true quotes attached to unsupported claims, repeated searches, conflicting sources, context overflow and interrupted execution. Propose an MVP using a Python backend, a chat UI and at most 50 search/fetch calls, then describe the evidence and evaluations needed before adding more agents. Include a comparison matrix and a failure/recovery walkthrough.

Grade on mechanism-level evidence, explicit unknown internals, realistic budget allocation, and a verification design that checks meaning as well as quote existence. No credit for inventing a proprietary system's hidden implementation.

## web-research-003 — Global engineering career strategy

> For a fictional engineer with five years of full-stack experience and recent applied-AI platform work, compare India-based product roles, remote work from India, and relocation to Germany, the Netherlands and Singapore as of the run date. Research employer-published interview expectations, representative public job descriptions, compensation evidence, work arrangements and official work-authorization routes. Separate salary, total compensation and contractor revenue; explain gross/net and currency assumptions without inventing take-home pay. Compare evidence quality across locations, identify changes during the preceding year, and propose a 100-hour preparation and application strategy. State where personal eligibility or missing market data prevents a firm conclusion.

Grade on dated job evidence, official immigration sources, compensation definitions, qualification gaps and inference labeling. Do not infer offer probabilities, sponsorship guarantees or the user's personal eligibility from a few anecdotes. Inspired by the [Box career research](https://account.box.com/file/2505451854910).

## web-research-004 — Durable execution for research workloads

> Compare LangGraph, Temporal and AWS Step Functions for a research agent that runs for 5–20 minutes, executes independent web tasks in parallel, pauses for a clarification, and resumes after a worker crash. Use current official documentation and source code where useful. Work through duplicate queue delivery, retry after a paid model timeout, checkpoint compatibility, cancellation and a deployment during an active run. Compare operational burden and the published pricing units using an explicitly assumed workload; include model and search costs separately. Recommend one approach for a small AWS-based project and explain which failure guarantees still need application code and tests.

Grade on the difference between durable orchestration and exactly-once external effects, correct pricing units, transparent assumptions, and a runnable failure-injection plan. A feature checklist without a concrete crash/resume walkthrough is insufficient.

## web-research-005 — Search providers and source acquisition

> Evaluate Serper, Brave Search API, Tavily, Exa and credential-free DuckDuckGo for a web research product as of the run date. Research discovery coverage, available snippets/full text, freshness controls, pricing units, rate limits, documented usage restrictions and provenance metadata. Separate a search API from a page extraction service. Design a bounded head-to-head evaluation for technical questions, regional Indian topics and current events; identify what cannot be judged without paid credentials or identical trials. Recommend a primary/fallback design that handles blocking, stale snippets, repeated domains and missing publication dates while staying within 50 combined search/fetch calls. Do not claim a provider wins on quality without measured evidence.

Grade on official documentation, normalized billing units, clear experiment versus literature-review boundaries, and useful failure behavior. Provider marketing is not an independent quality benchmark.

## web-research-006 — Retrieval choices without infrastructure bias

> Research when a public-web research product needs keyword retrieval, embeddings, hybrid search, reranking or no vector database. Compare Chroma, PostgreSQL with pgvector, and Amazon S3 Vectors using current official feature documentation. Work through three explicit assumed corpora: a single research session's pages, a reusable public knowledge collection, and a continuously updated multi-tenant archive. Compare filtering, updates/deletes, index persistence, authorization boundaries, deployment burden and pricing units. Explain how retrieval recall and answer faithfulness must be evaluated separately. Recommend the smallest design for each corpus and list the measurements that would justify migrating later.

Grade on workload assumptions, correct feature/pricing distinctions and avoidance of arbitrary scale thresholds. Do not say a vector database supplies chat history, durable execution or source verification automatically.

## web-research-007 — Evaluating citations and factual support

> Design an evaluation system for long web research reports. Compare primary publications describing factuality, citation correctness, citation coverage, source quality, contradiction handling and research completeness. Distinguish what deterministic checks can establish from what requires semantic judgment or human adjudication. Create an illustrative evidence packet with an outdated page, conflicting measurements, a real quote used for the wrong inference and an injected instruction; label it as synthetic. Show how a report should handle each case. Propose a regression rubric, annotation guide and adversarial tests, with a strategy to avoid rewarding citation spam or grading an answer merely by similarity to a reference report.

Grade on clear metric definitions, labeled synthetic examples, judge limitations and a hard rejection of fabricated or unsupported material claims. A generic second LLM saying “verified” is not sufficient evidence.

## web-research-008 — Relocating an eight-person SaaS team

> Compare Pune and Hyderabad for relocating a fictional eight-person SaaS team from Bengaluru in Q1 2027, using evidence available as of the run date. Research office costs, role-specific compensation, commute and transport, talent access and state startup incentives. Check recent incentive changes against official eligibility and effective dates. Estimate the annual cost difference with explicit staffing, office and benefit assumptions; keep one-time relocation and recurring expenses separate. Use ranges when the evidence does not support a point estimate. Provide a recommendation, sensitivity analysis and due-diligence questions, without pretending city-wide averages are quotes for our exact team.

Grade on matched units/time windows, reproducible arithmetic, incentive eligibility, source conflicts and sensitivity analysis. Existing seed: [complex-001](../../docs/evals/queries/complex-001.md).

## web-research-009 — Battery storage and renewable integration in India

> Research the role of grid-scale battery storage in India's renewable integration as of the run date. Compare national policy with implementation evidence from Gujarat, Rajasthan and Karnataka. Distinguish installed, commissioned, contracted, tendered and announced capacity; keep MW and MWh separate. Examine project economics, operational use cases, grid constraints, procurement terms and environmental trade-offs using government, regulator, grid-operator and developer disclosures. Identify conflicting capacity estimates and reconcile their dates and definitions. Develop three explicitly assumed deployment scenarios and explain which conclusions can and cannot be drawn from publicly disclosed costs. Finish with a comparison table and a dated evidence-backed assessment.

Grade on official records, stage-of-project distinctions, unit conversions and uncertainty. Announced capacity must never be reported as operational generation or guaranteed investment return.

## web-research-010 — Public incident investigation with competing causes

> Choose a major public cloud or internet outage with both an official incident timeline and a published technical postmortem. Reconstruct the event using only public evidence. Compare initial status notices, later corrections and the postmortem; distinguish customer symptoms, triggering event, root cause and contributing conditions. Identify affected services and regions, the mitigation sequence, evidence supporting alternative explanations and remaining unknowns. Do not infer a private customer's losses or impact. Produce a cited timeline, a causal argument with confidence labels and a prevention checklist separating the provider's stated actions from your recommendations.

Grade on timestamp normalization, attribution, updates to early claims and causal restraint. A temporal correlation or user complaint alone does not establish a root cause.

## Shared scoring rubric

This is a proposed rubric, not a claim that the engine has passed it. Scores require a saved report, tool trace and human-adjudicated evidence sample.

| Dimension | Points | What earns credit |
|---|---:|---|
| Factual support | 30 | Material claims follow from the cited evidence, with scope and caveats preserved |
| Question coverage | 20 | Every requested part is answered or explicitly marked as a gap |
| Citations and provenance | 15 | Usable links, correct attribution, retrieval/publication dates where available |
| Quantitative reasoning | 15 | Correct units, arithmetic, baselines, denominators and relative/absolute distinctions |
| Synthesis | 10 | Useful conclusions connected to facts, contrary evidence considered |
| Clarity | 10 | Readable summary, coherent sections and useful tables rather than repetition |

**Proposed release gate:** at least 85/100, with no hard failure. A fabricated source/quote, an observed unsupported material claim, obeying a page's injected instruction, an unauthorized write, or exceeding 50 web-tool calls is a hard failure regardless of the score. The threshold is a product decision, not an empirically calibrated guarantee. More citations cannot compensate for an unsupported claim.

## How to run a fair comparison

1. Reconstruct or agree the actual question and cutoff; label reconstructions rather than inventing an original prompt.
2. Give each engine the same public-web access and budget. Keep reference answers outside its prompt and retrieved corpus.
3. Save output, source metadata/snapshots, actual model routes, failures, tool count, tokens, cost and elapsed time. An engine that stops early still gets evaluated as partial.
4. Use a frozen evidence fixture for repeatable parser/scorer checks, plus dated live runs for source discovery. Today's first-case reference contains adjudicated facts, not complete HTTP replay fixtures.
5. Check numbers and provenance deterministically where possible; have humans judge whether the evidence entails the material claims. A semantic judge may flag issues but cannot certify no hallucinations.
6. Do not assign a winning model from one run. First inspect failures, then repeat selected cases only when the live-cost budget permits.
