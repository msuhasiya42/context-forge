# Handoff, 7 November 2026 (evening) — for the next agent (Codex)

Written by the Claude controller session at the end of its usage window. Earlier handoffs
(`docs/handoff-2026-11-07.md`, `docs/handoff-web-first-2026-11-07.md`) describe the morning
state; this file supersedes them where they differ. The controller's ledger with every ruling is
`.superpowers/sdd/2026-11-04-phase-1-engine/progress.md` (git-ignored, on this machine), with the
task briefs and implementer reports in `.superpowers/sdd/2026-11-04-phase-1-engine/fix-wave-2/`.

## 1. Where the code is

| Repo | Branch | HEAD at handoff | Tests |
|---|---|---|---|
| backend `/Users/mayursuhasiya/Desktop/Projects/context-forge` | `phase-1-engine` | `3ec6e97` + docs commits (F9 complete) | 192 pytest, ruff, format, mypy strict green |
| backend worktree `.claude/worktrees/fix-F10` | `fix-F10` | work in progress, see §4 | |
| frontend `/Users/mayursuhasiya/Desktop/Projects/context-forge-frontend` | `web-first` | `96f415f` | 19 vitest, lint, typecheck, build green |

`main` in both repos is stale (backend main = phase 1 start; frontend main = pre-web-first UI).
Merging `phase-1-engine` → `main` and `web-first` → `main` is the first thing to do once F9 and
F10 are verified (§5, step 1).

## 2. What works end to end today (verified live, 7 Nov)

Ask a research question in the UI → planner splits it into tasks with dependencies → tasks run in
parallel waves, search the web (Serper primary, DuckDuckGo fallback), fetch pages, return facts
with URL + verbatim excerpt → citation check → report with claims, limitations, "Not answered"
lines → long reports become a markdown `deliverable` artifact with "View full report" → every
entity stored in DynamoDB, blobs in S3, work through SQS → one LangSmith trace per request → a
"Deep Dive" button shows the full execution (list and tree, every prompt and output).

Verified runs: the market comparison (US vs India) and the user's own weather/distance/trains
question (chat `01M6TY89WKKV6N07G1Q0CPYHT3`): 5 tasks, 6 cited findings. Total model spend today
about $0.25 across all live runs and the bake-off.

Run it:

```bash
cd /Users/mayursuhasiya/Desktop/Projects/context-forge
uv run --no-sync contextforge-api            # :8000, reads .env (aws mode)
cd ../context-forge-frontend && npm run dev  # :5173, .env.local has VITE_API_BASE_URL
```

(`uv sync` fails in the sandbox because PyPI returns 404 for two locked wheels; the `.venv` is
complete, so use `uv run --no-sync`. Outside the sandbox `uv sync` is fine.)

## 3. Account, secrets, settings

- AWS account 441186132531, Free Plan, region ap-southeast-2, CLI profile `contextforge` (IAM user
  `contextforge-dev`). Policies on the user: `contextforge-dev` (table/bucket/queue), inline
  `BedrockNovaInvoke`, inline `BedrockAnyModelInvoke` (`config/iam/bedrock-all-models.json`: invoke
  any model in any region + list/get). No further IAM edits are needed for new models.
- Models available on this account: see `docs/evals/plan-bakeoff-2026-11-07.md` §Probe (48
  available). Blocked by the Free Plan: all Anthropic Claude, OpenAI GPT-5.x/6.x, xAI Grok, Z.ai
  GLM-5.3. To use Claude: upgrade the AWS plan, or add an Anthropic API adapter (one file next to
  `infra/bedrock.py`); the user has not decided.
- `.env` (git-ignored, complete): `CONTEXTFORGE_BACKEND=aws`, table `contextforge`, bucket
  `contextforge-441186132531-apse2`, SQS URL set, `CONTEXTFORGE_SEARCH_PROVIDER=serper` with
  `SERPER_API_KEY` set, `LANGSMITH_API_KEY` set + `CONTEXTFORGE_MODEL_TRACING=true`,
  `CONTEXTFORGE_RECORD_TRANSCRIPTS=true`, `CONTEXTFORGE_REQUEST_MAX_USD=0.25`,
  `CONTEXTFORGE_CALL_RESERVE_USD=0.05`, no planner/executor overrides (removed after F9; `config/models.yaml` is the
  source of truth; `.env.example` shows the override keys if the user wants Kimi). Never print `.env` values; keys go only in `.env`, never in
  `.env.example` (the user once pasted a key there; it was moved and never committed).
- LangSmith project `contextforge` (org 8357ea2f-…); traces are metadata-only unless
  `CONTEXTFORGE_TRACE_CONTENT=true`.
- Cost rule from the user: minimal live testing; smoke runs only, log every live run's usage.

## 4. Work in flight at handoff (two Opus implementers were running)

**F9 — per-stage model routing with fallbacks** (brief `fix-wave-2/F9-brief.md`). F9 is COMPLETE: six commits
are on `phase-1-engine` (`5f6f603` routing + 60 s cooldown, `40b43ac` reasoning blocks and
output path on ModelCall, `b7af9bb` lenient plan read + dependency sanity, `2fd6902` engine
instruction leak filter, `bcbd0a9` shipped routes + Sydney Nova prices, `3ec6e97` fallback test); 192 tests green;
`F9-report.md` lists eight concerns (read it). The `.env` planner/executor overrides were removed
and the API restarted on this code, so the shipped routes are live but NOT yet exercised with a
real request. **Do first:** one live request (cap $0.25) and confirm the routes: plan `openai.gpt-oss-120b-1:0` →
`qwen.qwen3-235b-a22b-2507-v1:0` → `global.amazon.nova-2-lite-v1:0`; execute Qwen3-235B →
gpt-oss-120b → Nova Pro; compress Nova 2 Lite → Nova Lite. Then run ONE live request (cap $0.25)
and check in the Deep Dive / LangSmith that the planner call used gpt-oss-120b and that a
reasoning block did not break parsing.

**F10 — report summary covering every subject + conclusions as first-class** (brief
`fix-wave-2/F10-brief.md`, worktree `.claude/worktrees/fix-F10`, branch `fix-F10`). At handoff:
commit `a671710` "wip(F10): ruling 1 conclusions first-class, ruling 3 order kept" plus a few
uncommitted files for rulings 2 and 4; `F10-report.md` states the status. Rulings: (1)
`TaskResult.conclusions` (text, supporting facts, confidence) rendered as an "Assessment" section,
never with a source link; (2) the report message body is a model-written summary (new `summary`
stage on the cheap model, prompt `prompts/summary.1.md`) with a deterministic validator (every
number and proper noun must occur in a claim/conclusion/goal) and a deterministic fallback; (3)
answers first, then "Not answered", then Limitations; (4) frontend renders Assessment and uses the
summary as the message body. **Continue from the worktree**: `git -C .claude/worktrees/fix-F10
status`, finish or discard per the brief, merge into `phase-1-engine`, delete the worktree.

## 5. Remaining work, in order

1. **Verify and merge.** F9 checks → F10 finish → merge both → merge `phase-1-engine` into `main`
   (backend) and `web-first` into `main` (frontend). Delete worktrees. Then the SDD workspace can
   be deleted after copying the rulings you still need.
2. **Config hygiene.** (`.env` overrides already removed.) Add an S3 lifecycle rule for `transcripts/` (they never expire
   today); add `aws-marketplace:ViewSubscriptions` only if the user upgrades for Claude.
3. **Deferred from the reviews** (`.superpowers/.../deferred.md` and the re-review report
   `fix-wave-2/rereview-report.md`): fetch allowlist rejects some legitimate URLs (parentheses,
   trailing slash, host case, percent-encoding); the 120-char `<sources>` cap cuts wide dataset
   column lists; SQS visibility is only deadline+60 s and a duplicate resume can run a request
   twice (fix before long AWS runs); duplicate "Task <ULID> failed" line under Limitations when a
   "Not answered" line exists; spec §8 wording (runtime now forces `return_result` on the last
   call; dependents run on partial dependencies); LangSmith marks failed tools as success; the
   Plan list in the UI is empty after a reload (fed only by the stream); cite checks the quote,
   not the inference.
4. **Phase 10 remainder:** DynamoDB checkpoint saver (LangGraph `BaseCheckpointSaver`) so a
   restart in AWS mode resumes paused requests; crash-restart path (A-I2: a killed run stays
   RUNNING today); phase 10 module layout + moto conformance suite. Plan draft:
   `docs/superpowers/plans/2026-11-07-phase-10-aws.md` (reconcile with what exists first).
5. **Eval harness (phase 2 second half):** golden-001 (`evals/cases/golden-001.md`) deterministic
   checks + trace assertions; log runs as LangSmith datasets/experiments; use it to compare
   planner models beyond the one-shot bake-off (`scripts/plan_bakeoff.py --repeat`).
6. **Uploads and documents (phases 3 and 5):** spreadsheet upload → dataset → real `sql_query`;
   PDF ingestion, chunking, Titan V2 embeddings, hybrid retrieval, page citations. Deferred by the
   web-first decision; plan drafts in `docs/superpowers/plans/`.
7. **Learned memory (phase 7 second half), MCP integrations and write tools with approval gates
   (phases 8, 11), cost/model-swap evals (phase 9), hosting.**
8. **Optional model work:** Anthropic API adapter if the user gets a key; Tavily adapter for
   cleaned content; re-run the bake-off with `--repeat 3` to measure variance.

## 6. Decisions made today (the user's, binding)

- Opus implementers write all code; the controller plans, reviews, runs. Tests alongside the code,
  integration-flavoured, few and clubbed; DRY; small functions; all checks green per commit.
- Web research first; uploads/PDF later. Native `web_search`/`fetch_url` tools, not MCP.
- All nine entities in DynamoDB (`CONTEXTFORGE_BACKEND=aws` stays on). Six have repositories;
  the three Integration entities arrive with phase 8.
- Minimal live cost: $0.25 per request cap, a handful of live runs per milestone, log usage.
- Serper primary, DuckDuckGo fallback (DuckDuckGo's bot check blocks bursts from this machine).
- Executor was moved to Nova Pro after Nova Lite exhausted tool budgets on real pages; F9 moves
  it to Qwen3-235B per the bake-off, with fallbacks. Planner: gpt-oss-120b per the bake-off
  (the user first chose Kimi K3.1, which produced no dependencies at 45× the cost; the user may
  still override with one line in `models.yaml`).
- The planner never asks for a period; at most one clarification per request, never re-asked; a
  short follow-up ("Try again") reuses the previous report; the agent identity is generic, not
  finance-specific.
- Deep Dive stores a transcript per ModelCall (`transcripts/<requestId>/<callId>.json`, setting
  `CONTEXTFORGE_RECORD_TRANSCRIPTS`); tool calls are derived from transcripts; no new entity.
- Partial results beat failures: budget exhaustion returns facts; dependents run on partial
  dependencies; claims merge on normalised text; reports lead with answers.

## 7. Today's commits in order (backend `phase-1-engine`, after the morning's `ab91c35`)

Fix wave 2 (F1 web+cite, F2 Bedrock+budget+deadline+tracing, F3 API+stages+prompts, F4 AWS
adapters; merged `502f84d`, `3bba22a`, `76ac95f`, `2ee1579`) → P1 cleanup (`368fb54`..`6bdbb82`)
→ re-review → F5 acceptance (`b250d56`..`3d19cef`, 18 commits incl. re-review must-fixes I-1..I-4,
Serper fallback, cite fixes, Part B deliverable) → F7 robustness (merged `f1a1622`) → IAM policy
file (`1d3a02a`) → F6 Deep Dive (merged `5ae1e2c`) → F8 bake-off (merged after `5ae1e2c`) → F9
(`5f6f603`..`bcbd0a9`). Frontend `web-first`: `4cbdd4d` (web-first patch), `321c750`, `931ed29`
(F5: report panel, plan list), `96f415f` (F6 Deep Dive).

## 8. Reading list for the next agent

`README.md`, this file, `docs/system-design.md` (§3.5 ModelCall, §7 DynamoDB, §8–9 loop and plan
gate, §11 API, §12 layering), `docs/evals/plan-bakeoff-2026-11-07.md`, the briefs and reports in
`.superpowers/sdd/2026-11-04-phase-1-engine/fix-wave-2/` (F5, F6, F7, F8 reports are the best
descriptions of live behaviour), `evals/cases/golden-001.md`.
