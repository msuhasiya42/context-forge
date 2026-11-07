# Research agent continuation — 7 November 2026

Current build marker: **web-research-v3**, including the later live-failure repairs and API
runner below. This supersedes the evening handoff for the working changes described here. Backend branch:
`phase-1-engine`, based on `0092b86`; changes are uncommitted. The sibling frontend was not
modified. Product priority: demonstrate strong public-web research through saved, evaluated
reports before expanding uploads, PDF, private MCP, write tools or learned memory.

## Implemented and checked offline

- Narrow planner repair for `kind: plan, question: []`; clarification still requires text.
- Atomic request-wide search/fetch reservations, including failures, persisted as
  `RequestMessage.webToolCalls` in memory/DynamoDB. `webToolCallBudget: 50` is separate from
  model-call limits. Web tasks default to 12 local tool calls, still inside the shared cap.
- Coverage-oriented prompts and final citation input rebuilt from all task outcomes, rather
  than only the truncated request state. Prior reports supply assessments and gaps too.
- Safer URL equivalence, explicit publication/update metadata, retrieval time, publisher,
  title and a hash of bounded extracted page text. Missing publication dates stay missing.
- Snapshot/quote checks followed by numerical-presence and semantic support review. Unknown,
  duplicate, missing or unavailable reviews withhold claims. Summaries, assessments and every
  drafted report heading/paragraph are checked too. This is a filter, not truth certification.
- A research writer that references retained claim IDs. Source links are rendered from saved
  evidence; invented references or unsupported drafts fall back to cited findings.
- Failed returned tool results now trace as failures. Health exposes `build: web-research-v3`,
  `processId` and `webToolCallLimit` to distinguish a loaded build from stale code.
- Read-only evaluation capture/grading CLI. Captures do not start research. Quality passes
  require exact-bundle supplied adjudication, rubric score at least 85 and no hard failures;
  structural checks alone yield `needs_review`.

- Reproduced the Python release-date failure from saved checkpoint outcomes and the visible
  source snapshot. The model inserted sentence periods between source lines. Cite now recovers
  the actual contiguous source span only when those periods differ; words/numbers/operators
  still have to match. Zero-finding web reports fail, and comparisons lose approval when any
  declared supporting finding is missing, during both grounding and final citation checks.
- Web-dependent reconcile tasks now invoke the validated model loop without search tools.
  The legacy non-web reconciliation placeholder remains outside this research milestone.
- Added the normal-API submit/watch client and rotating contextual, redacted `backend.log`.
  See [API testing](api-testing.md). A stale build is rejected before submission; all records
  are the same ones the UI reads. Tests use real routes/background work with synthetic providers.

Latest verification: **226 pytest tests passed**; Ruff lint and formatting passed; strict mypy
passed over 83 source files. One Starlette/AnyIO deprecation warning remains. The full workflow
test uses a scripted model and fake HTTP to exercise budget exhaustion, wrong-metric rejection,
report rendering, API capture and grading. It does not measure live model intelligence.

## Immediate next action: live acceptance

The last Brave health check showed the old build, without the new build marker. The API has
no autoreload. The user authorized stopping/restarting it, but shell socket operations are
denied and computer use explicitly denied access to Terminal. Do not kill the running server
without a working replacement route. In its existing terminal, stop it and run:

```sh
uv run --no-sync contextforge-api
```

Confirm `http://localhost:8000/health` shows `web-research-v3`, then run the brief Python release
date question in [the benchmark instructions](evals/web-research-running.md). Keep the existing
**$0.25 request cap**. Save routes, web calls, estimated spend, citations and gaps. No new live
research acceptance or ContextForge-versus-Box quality score has been earned by this build.

## Research quality milestone

1. Pass the small live acceptance question.
2. Run and capture case 001 with references excluded from agent inputs.
3. Review every material claim and the narrative; compare against the selected reference and
   Box report using the six-dimension rubric. Preserve failures and partial coverage.
4. Improve the weakest measured dimension, then rerun only the checks affected by the change.
5. Expand the remaining cases and adversarial/follow-up coverage incrementally.

Reference material: [research plan](research-first-plan.md), [ten cases](../evals/cases/web-research-suite.md),
[reference report](research/ai-cost-engineering-report-2026-11-07.md),
[selected-claim Box comparison](evals/ai-cost-box-comparison-2026-11-07.md).

## Remaining limitations

- Nine cases are specifications, not completed factual golden answers. Case 001 has 12 selected
  reference checks, not complete original HTTP replay fixtures. No ten-case pass is claimed.
- Source independence, contradiction resolution, live discovery coverage and useful report
  depth still need measurement. Prompt-driven adaptation exists within tasks; explicit global
  replanning and structured report tables have not been implemented.
- Numerical presence guards can reject valid derived calculations; a verified calculation
  pathway remains work. Semantic review is metered and can exhaust the same request budget.
- Durable web accounting does not fix cloud graph checkpoints, duplicate-resume races, SQS
  visibility renewal or stuck-work recovery. Real AWS failure injection remains pending.
- Plan restoration, cumulative web usage and complete stage visibility in the frontend remain
  work; this session has read-only access to that sibling repository. S3 transcript retention
  was not changed. No AWS resources, policies, merges or deployments were made here.
