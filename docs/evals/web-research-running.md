# Running the web research benchmark

The benchmark separates offline software checks, dated live engine runs and claim-by-claim
adjudication. A passing software test or a valid citation is not a research-quality score.

## Load the current backend

In the API terminal, stop the existing process with Ctrl+C, then run from the backend root:

```sh
uv run --no-sync contextforge-api
```

Open `http://localhost:8000/health`. This build returns `build: web-research-v3`, a process ID,
and `webToolCallLimit: 50`. The API has no automatic reload. Refreshing the browser alone does
not load changed Python code. The current agent session cannot open server sockets or control
Terminal, so an operator must do that restart.

Keep the existing $0.25 live-request cap. The new review and report passes are metered calls
within the same spend/model/deadline limits. Exhausted or unavailable reviews withhold claims
or use the findings fallback; they do not silently become successful verification.

## First acceptance check

Use [the API runner](../api-testing.md) to submit and capture a run without manual UI input.
It saves a normal chat that can also be inspected in the UI. The older capture command below
remains useful for an already completed run.

Ask one small question before an expensive benchmark:

> Find the official release pages for Python 3.12.0 and Python 3.13.0. Give each release date
> with a source link, then assess which is newer based on those dates. Keep the answer brief.

Inspect the result and Deep Dive. Confirm one valid planner result (including a logged lenient
repair if the model sends `question: []`), web evidence, retained source-supported dates and a
qualified comparison. Record actual model routes, tool count, estimated spend, gaps and the
report. Do not equate successful parsing with a correct answer.

## Capture an existing run without starting another

After running [case 001](../../evals/cases/web-research-suite.md) in the app, use the chat and
request message IDs from the API response/Deep Dive network payload:

```sh
uv run --no-sync python -m contextforge.evals.web_research capture \
  --chat CHAT_ID --request REQUEST_MESSAGE_ID --case web-research-001 \
  --out .data/evals/web-research-001-run.json
```

This uses authenticated GET routes only. It captures the existing detail, report, evidence
snapshots, full deliverable and trace. It never queues research or calls a model. A missing
report or expired snapshot is an explicit capture error. `.data/` is ignored by Git; captures
can contain a user's question and fetched text.

## Grade structural integrity

```sh
uv run --no-sync python -m contextforge.evals.web_research grade \
  --bundle .data/evals/web-research-001-run.json \
  --out .data/evals/web-research-001-grade.json
```

Checks cover case/request ownership, completion, available findings, the durable web counter,
recorded spend and URL/excerpt matches against captured evidence. An older capture with no
web counter is unknown, never assumed to have used zero calls. A quote with the wrong meaning
still requires semantic review. Reference values guide the reviewer; numeric/phrase matches
do not earn a quality pass.

Without annotations the status is `needs_review` (or an explicit failure/incomplete capture),
and the command exits 2. It must not be reported as a successful golden run.

## Add adjudicated support labels

Use the returned `bundleHash` to bind reviews to the exact report, full deliverable and source
capture. Review each claim once, inspect the narrative, and score using [the shared rubric](../../evals/cases/web-research-suite.md).
Example shape, with placeholders that must be replaced after review:

```json
{
  "bundleHash": "HASH_FROM_GRADE",
  "reviewer": "REVIEWER_NAME",
  "narrativeReviewed": true,
  "claims": [
    {"claimId": "ACTUAL_CLAIM_ID", "supported": true, "reason": "Specific source and scope checked."}
  ],
  "flags": {
    "fabricated_sources_or_quotes": false,
    "unsupported_material_claims": false,
    "injection_followed": false,
    "unauthorized_writes": false
  },
  "scores": {
    "factual_support": 0,
    "question_coverage": 0,
    "citations_provenance": 0,
    "quantitative_reasoning": 0,
    "synthesis": 0,
    "clarity": 0
  }
}
```

Then add `--annotations PATH_TO_REVIEW.json` to the grading command. Only a structurally valid
run with complete matching annotations, no hard failure and a score of at least 85 receives
`passed_with_supplied_reviews` (exit 0). This records supplied adjudication; it does not
authenticate the reviewer's identity or independently certify factual truth.

## Judged suite runs (offline LLM judge)

`backend/evals/cases/core-suite.yaml` holds the core cases (`id`, `question`, `requiredParts`,
`source`). The judge (`config/models.yaml` stage `judge`: Gemini 3.5 Flash, falling back to
Gemini 3.5 Flash-Lite; while every research stage also runs on Gemini (F17) the CLI warns that
the judge grades its own model family, and without `CONTEXTFORGE_GEMINI_API_KEY` it refuses) labels each claim `supported`, `partial`,
`unsupported` or `contradicted` against its cited excerpt and the saved page text around it,
marks each required part `answered`, `partial` or `missing`, and scores quantitative reasoning,
synthesis and clarity 1-5. These map onto the rubric above; citations and provenance come from
the structural checks. A judged run passes at 70 with no flag; an unsupported or contradicted
claim raises `unsupported_material_claims`; judge errors on more than 20% of claims make the
run `incomplete`. A human annotation for the same `bundleHash` wins for each field it sets.
The judge never runs inside a user request.

The Gemini models run on Google's free tier through its OpenAI-compatible API, so judging costs
about $0 and the grader is of another model family than the pipeline. Set
`CONTEXTFORGE_GEMINI_API_KEY` in `backend/.env`; calls are spaced at least
`CONTEXTFORGE_GEMINI_MIN_INTERVAL_SECONDS` (default 4.5) apart for the free tier's request limit,
so a suite takes a few seconds per judge call longer. A throttled (429), unavailable (404, 5xx)
or timed-out Gemini call moves to the next model, ending on Bedrock DeepSeek at its price;
without the key the judge skips Gemini and uses DeepSeek (logged once).

With the API running (as above), from `backend/`:

```sh
# Prints the estimate and submits nothing; add --yes to run.
uv run --no-sync python -m contextforge.evals.web_research suite run \
  --suite evals/cases/core-suite.yaml --repeats 3 --max-usd 6.00
# Re-grade saved runs with the current judge (judge spend only).
uv run --no-sync python -m contextforge.evals.web_research suite grade .data/evals/SUITE_RUN_ID
# Regression gate: exit 1 when the mean drops over 3 points, pass^k drops or a new flag appears.
uv run --no-sync python -m contextforge.evals.web_research suite compare \
  .data/evals/BASELINE/scorecard.json .data/evals/CANDIDATE/scorecard.json --fail-on-drop 3
```

Each suite run writes `.data/evals/<suite-run-id>/runs/*.json` (bundle, case, grade, research
cost, tokens and latency), `scorecard.json` and `scorecard.md`. pass@1 is the share of runs
that pass; pass^k is the share of cases whose k repeats all pass. The suite stops submitting
new runs once research plus judge spend reaches `--max-usd` (default 3.00), and the scorecard
says so.

Before trusting the judge, calibrate it against your own labels:

```sh
uv run --no-sync python -m contextforge.evals.web_research calibrate export .data/evals/SUITE_RUN_ID --n 20
# Fill human_label in calibration.csv, then compare models (default: the judge stage's model and each fallback):
uv run --no-sync python -m contextforge.evals.web_research calibrate score \
  .data/evals/SUITE_RUN_ID/calibration.csv
```

## FRAMES benchmark

A public multi-hop benchmark runs through the same suite runner: see [frames.md](frames.md).

## Current evidence and limits

The full offline workflow is covered with a scripted model, real web-tool adapters over fake
HTTP responses, the shared budget, support gates, report rendering, API capture and grading.
Those fixtures are synthetic and test enforcement, not live model intelligence. Case 001 has
12 selected researched reference facts; complete original HTTP replay captures and the nine
other cases' factual answer sets remain pending. No live ContextForge-versus-Box quality score
has been earned yet.
