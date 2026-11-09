# FRAMES benchmark slice

FRAMES (Factuality, Retrieval, And reasoning MEasurement Set) is Google's benchmark for
retrieval-augmented answering: Krishna et al. 2024, "Fact, Fetch, and Reason: A Unified
Evaluation of Retrieval-Augmented Generation" ([arXiv:2409.12941](https://arxiv.org/abs/2409.12941)).
The dataset is [`google/frames-benchmark`](https://huggingface.co/datasets/google/frames-benchmark)
on Hugging Face, licensed Apache-2.0: 824 multi-hop questions in one `test` split. Each has a
short gold answer, the Wikipedia pages needed to answer it, and its reasoning types
(numerical, tabular, multiple constraints, temporal, post-processing).

## The setting we run

The paper's oracle setting gives the model the gold Wikipedia pages. We do not. Each question is
submitted unchanged to the running ContextForge API, which plans, searches and fetches on the
open web with its own tools, exactly as for a user. The research pipeline and its prompts are
not changed for the benchmark; the report is read afterwards by the offline judge (the `judge`
stage in `config/models.yaml`, which shares no model with any research stage).

For each run the judge:

1. extracts the shortest answer the report states, and the report claims it rests on
   (`prompts/judge_frames_extract.1.md`); a report that gives no answer, or lists candidates
   without choosing, has none;
2. grades that answer against the gold one as `correct`, `incorrect` or `no_answer`, by meaning
   rather than wording (equivalent numbers, names, dates and units), following the paper's
   autorater (`prompts/judge_frames.1.md`);
3. for a correct answer, checks the claims behind it against their cited page snapshots with the
   F13 claim judge. The answer is **supported** when at least one of them is `supported`.

## Metrics

- **accuracy**: correct answers over judged runs (the paper's headline metric).
- **supported_accuracy** (`supportedAccuracy`): correct and supported answers over judged runs.
  The gap between the two is answers the model knew but did not find on the web.
- **no-answer rate**: runs with no answer, or no captured report, over judged runs.
- **accuracy by reasoning type**: a question counts under every type it lists.
- **pass^k** when `--repeats` is above 1: the share of questions answered correctly in every
  repeat.
- Per question: the answers, verdicts, research cost, tokens and latency, and judge cost.

Runs where the judge itself failed are left out of the ratios and counted as judge errors.
The F13 scorecard fields are kept (`meanScore` is accuracy x 100; a run passes when its answer is
correct), so `suite compare` works on FRAMES scorecards and also prints accuracy, supported
accuracy and no-answer rate deltas.

Gold answers are as of the dataset's creation (2024). A question whose real-world answer has
since changed may be marked incorrect; the judge then sets `possiblyStale`, which is counted on
the scorecard but does not change the verdict.

## Commands

Run from `backend/` with the API running (`uv run --no-sync contextforge-api`, `/health` reports
build `web-research-v3`).

```sh
# Estimate only: downloads nothing, submits nothing, calls no model.
uv run --no-sync python -m contextforge.evals.web_research bench frames --n 30 --seed 7 --max-usd 8.00
# Run it. The first run downloads the split once to .data/benchmarks/frames/test.jsonl
# (with the dataset revision in test.meta.json); later runs reuse the cache.
uv run --no-sync python -m contextforge.evals.web_research bench frames --n 30 --seed 7 --max-usd 8.00 --yes
# Ask exactly the questions of an earlier run (its frames-sample.json).
uv run --no-sync python -m contextforge.evals.web_research bench frames \
  --sample-from .data/evals/<run-id> --max-usd 8.00 --yes
# Re-grade saved runs with the current judge; no research spend.
uv run --no-sync python -m contextforge.evals.web_research bench frames --regrade .data/evals/<run-id>
# Compare two FRAMES scorecards and gate on an accuracy drop of more than 5 points.
uv run --no-sync python -m contextforge.evals.web_research suite compare \
  .data/evals/<baseline>/scorecard.json .data/evals/<candidate>/scorecard.json --fail-on-drop 5
```

The sample is stratified by each question's first reasoning type: every type gets at least one
question and the rest are shared in proportion, so 30 questions with seed 7 cover all five
types. Output goes to `.data/evals/<run-id>/`: `frames-sample.json` (dataset, revision, seed and
ids), `runs/frames-<row>-r<n>.json`, `raw/`, `judge-transcripts/`, `scorecard.json` and
`scorecard.md`.

## Cost

Research is bounded by the per-request cap (`CONTEXTFORGE_REQUEST_MAX_USD`, $0.25 locally), so
30 questions cost at most $7.50 in research. The judge costs about $0.01 per question (about 15k
input and 800 output tokens at DeepSeek v3.2's prices), about $0.30 for 30. The bench stops
submitting once research plus judge spend reaches `--max-usd` (default $5.00, which can stop a
30-question slice after about 20 questions if every request hits the cap). All figures are
recorded estimates, not billing.

## License and attribution

The FRAMES dataset is © Google, released under Apache-2.0. We download it from Hugging Face at
run time and do not redistribute it; the test fixture `backend/tests/fixtures/frames-rows.json`
holds six rows copied from it, under the same license. Cite: Krishna et al. 2024, *Fact, Fetch,
and Reason: A Unified Evaluation of Retrieval-Augmented Generation*, arXiv:2409.12941.
