# AI cost engineering: comparison with the Claude report in Box

[Read this comparison in Box](https://app.box.com/file/2576817388675).

Compared on **7 November 2026**:

- **Baseline:** [AI Cost Engineering Industry Research Report (Nov 2026)](https://account.box.com/notes/2572723427786), Box file `2572723427786`, version `2855611403786`, SHA-1 `482ad5416f427565a7d5ba604db93ed733c9e8f2`. The user identifies this as Claude-produced research. Box modification date: November 5, 2026.
- **New reference:** [Codex research report](../research/ai-cost-engineering-report-2026-11-07.md), researched from public primary sources. This is not a new ContextForge engine output.

The baseline was read before the new research. The original prompt, tool trace, source snapshots, cost and model configuration for the Box report were unavailable. This is a scoped manual evidence audit and editorial comparison, not a blinded head-to-head experiment or a measured agent ranking.

## Overall finding

The Box report is a useful **format and depth reference**: it develops the topic through a short summary, company cases, architecture comparisons, reusable patterns, practical guidance and caveats. The new report retains that progression while making metric boundaries, historical dates and research limitations more explicit. No overall numerical quality score is assigned without a complete, reproducible claim inventory and independent grading.

## Selected claim audit

| Topic | What the Box report does | What the primary-source check found | Treatment in the new reference |
|---|---|---|---|
| Uber | Reports request/session cost reductions and a fixed-model comparison | Selected headline values are supported; body and chart disagree on endpoint wording | Preserve different baselines and endpoint ambiguity; do not infer total-spend savings. [Source](https://www.uber.com/us/en/blog/efficient-software-factory/) |
| Manus | Calls the cached-input price difference a cost lever | The historical ratio is supported; no whole-agent savings percentage is established | Keep price ratio separate from measured spend and current tariffs. [Source](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) |
| Anthropic tool search | Repeats the publisher's reduction label | The label and rounded before/after counts are not an exact arithmetic match | Attribute the label and flag the arithmetic mismatch instead of silently reconciling it. [Source](https://www.anthropic.com/research/advanced-tool-use) |
| Code execution | Includes a large illustrative token saving and labels it a best case | The source discusses tool discovery/loading and large-result handling as separate mechanisms | Specify which example the number belongs to; do not generalize it to all transfer workflows. [Source](https://www.anthropic.com/research/code-execution-with-mcp) |
| Multi-agent research | Reports better internal-eval results and higher token usage | Supported as vendor-reported, workload-specific comparisons | Keep the chat-token denominator and avoid treating the performance uplift as absolute accuracy. [Source](https://www.anthropic.com/research/multi-agent-research-system) |
| Retrieval | Labels the failed-retrieval metric and warns that a top-five variant is hypothetical | Supported in the tested top-20 setup | Retain this strong caveat; never rename it hallucination reduction. [Source](https://www.anthropic.com/research/contextual-retrieval) |
| Cursor | Distinguishes first-party and partner speed claims | Throughput and reuse architecture are supported; dollar savings are not quantified | Separate decoding speed, indexing latency and expense. [Speed source](https://fireworks.ai/customers/cursor-fast-apply-speculative-decoding), [indexing source](https://cursor.com/blog/secure-codebase-indexing) |
| Intercom | Includes the model-selection story, newer specialization and a secondary podcast account | The selected primary pages support task-specific model choice and specialization; the podcast savings were not reverified | Add the narrow GPT-4.1/GPT-4o evaluation comparison, retain Apex as a company claim, omit unrechecked podcast amounts. [Case study](https://openai.com/index/intercom/), [Apex announcement](https://www.intercom.com/blog/introducing-the-fin-api-platform) |
| Plan caching | Reports average savings and retained relative performance | The APC paper supports those values on its evaluated workloads | Preserve the relative-performance denominator; do not call it absolute accuracy. [Paper](https://arxiv.org/html/2506.14852v2) |
| OpenAI caching | Includes older maximum-discount guidance and warns that pricing changes | Current documentation now advertises up to 95% cached-input discount, model-dependent | Treat this as a dated documentation difference, not proof the earlier report hallucinated. [Current docs](https://developers.openai.com/api/docs/guides/prompt-caching) |

## Editorial comparison

| Dimension | Box baseline | New reference | What ContextForge should learn |
|---|---|---|---|
| Breadth | More company examples, emerging research and interview questions | Narrower primary-source audit of the core mechanisms | Plan the requested coverage; do not pad with weakly sourced companies |
| Readability | Detailed narrative, tables and reusable patterns | Shorter cases, a metric table, explicit implications | Deliver a short chat summary plus a complete structured report |
| Claim labeling | Explicit verified/secondary/inference distinctions | More cautious company-reported/benchmark/capability/inference labels | A company's own statement establishes attribution, not independent truth |
| Citation usability | Key-source list and named publications in the retrieved text | Direct links beside claims and table conclusions | Preserve usable links through export; Box text extraction can lose formatting |
| Numerical reasoning | Strong quantitative breadth, illustrative calculations labeled | Selected calculations and denominator conflicts checked explicitly | Check units, baselines, dates, arithmetic and relative versus absolute changes |
| Missing evidence | Names companies with insufficient evidence and several secondary sources | Adds explicit unaudited claims and unavailable experiment artifacts | Report a coverage gap instead of manufacturing completeness |
| Practical relevance | Enterprise analytics platform and interview preparation | Research-only ContextForge priorities | Tailor recommendations to the user's actual workflow |

The text representation returned by Box flattens much of the Box Note's formatting. Citation placement and visual presentation cannot be graded conclusively from that extraction alone. The source list was usable for investigation; the comparison does not claim the original Box UI lacked links or structure.

## What this does and does not establish

The audited subset supports much of the baseline's central argument. We found scope qualifications and a current-documentation update, not evidence that the entire baseline is unreliable. Some source inconsistencies originate in publisher pages themselves. A careful report must preserve those uncertainties.

This work establishes a **reference answer and a grading approach**. It does not establish that ContextForge currently matches either report. A fair next engine experiment must provide the same reconstructed prompt, cutoff and maximum of 50 combined search/fetch calls, capture the actual result and trace, then score against source-backed checks. The earlier Python release-date smoke failed at planning and is not a research-quality result.

## Acceptance criteria for our agent

Require supported material claims, coverage of every requested section or an explicit gap, correct numerical scope, clear separation of conclusions, and no invented sources or quotes. The full rubric and ten complex prompts are in [the web research suite](../../evals/cases/web-research-suite.md). The Box report is a reference for style and coverage; source-backed adjudicated claims are the factual reference.
