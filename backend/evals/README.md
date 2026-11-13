# ContextForge evals

One question: **for each model configuration, what quality do we get, and at what cost?**

Code: `src/contextforge/evals/harness/`. Data and results: this folder.

## Datasets

All are fixed. Each JSONL's SHA-256 is stored in its `.meta.json` and in the LangSmith dataset's
metadata, and the LangSmith version is tagged `v1`. A changed dataset is refused; it must get a
new name, so old scores stay tied to the data they were measured on.

| Dataset | What | Source | Sampling |
|---|---|---|---|
| `cf-frames-dev-20` | 20 multi-hop questions with a short gold answer | [`google/frames-benchmark`](https://huggingface.co/datasets/google/frames-benchmark) (Apache-2.0), revision `58d9fb63` | seed 42, stratified by first reasoning type: 7 multiple constraints, 6 numerical, 4 tabular, 2 temporal, 1 post-processing |
| `cf-drb-en-5` | 5 open research tasks with an expert reference report and RACE criteria | [DeepResearch Bench](https://github.com/Ayanami0730/deep_research_bench) (Apache-2.0), commit `f2735b5c` | 5 of the 50 English tasks, seed 42, one per randomly drawn topic: Hardware, Art & Design, Games, Entertainment, Transportation (task ids 83, 87, 89, 91, 94) |
| `cf-handmade` | Questions from live runs, with a gold answer and the parts a full answer must cover | written by hand | added one at a time; the first is the EU AI Act GPAI question from 13 Nov 2026 |

Example shapes:

- FRAMES: inputs `{question}`; outputs `{answer, wiki_links}`; metadata `{case_id, source_row, reasoning_types}`.
- DRB: inputs `{question}`; outputs `{reference_report, dimension_weights, criteria}` (the benchmark's own per-task criteria and weights); metadata `{case_id, task_id, domain}`.
- Handmade: inputs `{question}`; outputs `{answer, required_parts}`; metadata `{case_id, written, origin, gold_sources}`.

FRAMES gold answers are as of the dataset's creation (2024), so a question about something that
changed since can be answered "wrongly" by a correct, current report.

## Rebuild

```bash
uv run python -m contextforge.evals.harness.datasets build   # JSONL from the local caches (offline)
uv run python -m contextforge.evals.harness.datasets push    # create missing datasets in LangSmith
uv run python -m contextforge.evals.harness.datasets show    # print 2 examples of each, from LangSmith
```

`build` needs the caches: FRAMES downloads itself into `.data/benchmarks/frames/` on first use;
DRB's three files (`data/prompt_data/query.jsonl`, `data/test_data/cleaned_data/reference.jsonl`,
`data/criteria_data/criteria.jsonl`) go in `.data/benchmarks/drb/raw/`, downloaded from the
commit above. A rebuild that would produce different contents is refused.
