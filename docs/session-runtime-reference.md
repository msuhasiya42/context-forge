# Optional: current-runtime code walkthrough

The main session is [the login incident](session-examples/login-incident/README.md). This separate architecture-question fixture exercises current web tools; it does not execute the proposed incident integrations.

# Context engineering and internal execution — session walkthrough

Audience: experienced engineers. Duration: 60 minutes. Begin where the architecture diagram
ends: a user's question has been accepted. Follow one request through the backend, inspecting
the context at each model call. Start with the [login incident JSON walkthrough](session-examples/login-incident/README.md)
for the selected session scenario, including artifact offload and proposed read-back. The
architecture example below demonstrates the existing runtime separately. Its prompt remains in
[session-architecture-decision.txt](../evals/cases/session-architecture-decision.txt).

## Open with the connection to the high-level design

“We've discussed the services and storage. Now we'll inspect what happens inside the worker:
which instructions and data reach each model call, how a plan becomes stored tasks, how tool
results enter the next call, and how we decide which findings can appear in the answer.”

Bridge the older diagram to current names in one minute:

- Its Job is now represented by the initial request Message; no separate Job entity.
- A request has Tasks; each Task holds its result. A report is another Message.
- Web evidence and deliverables have Artifact records, with content stored separately.
- ModelCall records describe individual model attempts and point to stored transcripts.
- The implemented research path uses native `web_search` and `fetch_url`. PDF/vector retrieval,
  private MCP, proposed writes and learned memory in the older diagram are later scope.
- The stream-to-Lambda-to-SQS bridge in the diagram is a planned path; the current application
  submission explicitly sends work to its queue. The diagram's throughput figure is a design
  assumption, not a benchmark achieved by this project.

## Session timing

| Minutes | Show | Explain |
|---|---|---|
| 0–5 | One accepted request and the architecture question | Map the diagram's Job to the current request Message |
| 5–10 | Request Message versus initial request state | Persisted conversation data and working execution data have different roles |
| 10–22 | Complete planner input, output schema, response and stored Tasks | Context assembly, structured output validation and reference-to-ID mapping |
| 22–35 | Three executor calls side by side | Model → search → model → fetch → model → task result |
| 35–43 | Context before/after result stubbing | Context selection, bounded history, artifact references and summarization |
| 43–50 | Evidence checks and report contract | Quote integrity, semantic support and inference prerequisites |
| 50–60 | Questions with trace open | Retries, failures, cost, durability and scope |

## Generate full JSON examples for the presentation

From the backend repository root:

```sh
uv run --no-sync python -m scripts.export_session_context
```

Open `.data/session-context/00-READ-FIRST.json` first. This example uses the actual planner
context builder, plan validators, task persistence, executor graph, web-tool adapters and
Bedrock request formatter. Model responses and HTTP results are controlled fixtures. No model
inference or external HTTP request is performed. Only planning and the first task execute;
other tasks remain planned and there is no final architecture report. It demonstrates
mechanics, not research quality or a recommendation about these platforms.

The generated Bedrock requests are adapter output, not requests sent to Bedrock. A live model
can take different routes, and compatibility fallbacks can alter its provider payload.
Generated source text is explicitly synthetic, including filler for the compaction example.

| File | What to highlight |
|---|---|
| `01-request-message.json` | Message ID, Chat ID, kind, sender, content, status and web counter |
| `02-initial-request-state.json` | Goal/plan/facts start empty; identity and memory snapshot already exist |
| `call-01-plan-transcript.json` | Full system blocks, assembled user context, planner schema and scripted response |
| `call-01-plan-bedrock-request.json` | Provider payload: `system`, `messages`, `toolConfig`, `return_result` schema |
| `03-validated-plan.json` | Validated goals, kinds, local refs and dependency refs |
| `04-persisted-tasks.json` | Generated task IDs, mapped dependency IDs, request ID, PLANNED status |
| `05-request-state-with-plan.json` | How the validated plan enters working request state |
| `call-02-execute-transcript.json` | Task-specific context, allowed tool definitions and first search request |
| `call-03-execute-transcript.json` | Assistant search call and matching tool result now appear in history |
| `call-04-execute-transcript.json` | Search plus fetched-page history; structured task result with evidence refs |
| `call-03-execute-bedrock-request.json` | How the adapter maps tool results to Bedrock's conversation format |
| `06-first-task-outcome.json` | The compact result returned to the outer workflow |
| `07-model-call-records.json` | One record per scripted invocation, separate from user Messages |
| `08-artifact-records.json`, `09-source-snapshots.json` | Artifact metadata versus saved source content |
| `10-history-before-stubbing.json`, `11-history-after-stubbing.json` | Full source text becomes a compact reference in an illustrative copy of history |
| `12-context-size-estimates.json` | Estimated history size before/after, with the demonstration policy stated |

All files contain full JSON. During the talk, expand one relevant subtree at a time; keep
the entire packet available for questions. Lead with “what changed since the previous call?”
rather than reading every field aloud.

## Checkpoint 1: request, state and model input are different objects

The saved request Message is not itself the entire model prompt. `RequestState` accumulates
execution information, while `ContextInputs` selects the data for a particular model call.
`build_context` renders that selection. Source records in storage do not automatically become
model-visible text.

Open [request_state.py](../src/contextforge/worker/request_state.py) and
[builder.py](../src/contextforge/core/context/builder.py). Ask: “Which fields would we include
for planning, and which would be unnecessary or misleading?”

## Checkpoint 2: planner prompt and roles

The planner context is assembled in `_planner_inputs` and `_call_planner` in
[plan.py](../src/contextforge/worker/stages/plan.py).

In the exported planner transcript, inspect:

```text
request.system[0]       procedural instructions + any standing user instructions
request.system[1]       active planner instructions from plan.5.md
request.messages[0]     assembled user/context text, including the request
request.outputSchema    PlannerOutput schema
request.tools           no research tools at the application-model boundary
```

Inside the user text, expand `<request>`, `<context>`, `<user_preferences>`, `<constraints>`,
`<sources>` and, when available, `<prior_context>`. Optional sections can be absent. The builder
uses the historical section label `brief` internally for a group of request-context fields;
the Python variable is `request_state`.

There is no requirement to invent an assistant message for the first call. Assistant messages
are produced by model responses; executor history grows as those responses and tool results
are retained. The planner currently renders selected earlier requests/reports inside its
assembled prior-context section rather than blindly replaying the whole chat.

The output schema becomes a `return_result` tool in our normal Bedrock payload. Thus the
planner has no search/fetch tools, but still has a structured-result mechanism at the provider
boundary. In the payload, find the forced result tool choice and its JSON schema.

Open [bedrock.py](../src/contextforge/infra/bedrock.py), `converse_payload`. System blocks are
separate from messages in this API. The application transcript calls tool-response messages
`role: tool`; our Bedrock adapter encodes native tool results as `toolResult` blocks inside a
`user` message. Inspect both layers rather than assuming a provider-independent role layout.

A transcript contains normalized application requests/responses, not every raw provider
response field or HTTP header. Model inputs and outputs reveal observable execution; they
do not provide complete access to the model's internal computation.

## Checkpoint 3: planner JSON becomes Tasks

Open `PlannerOutput` in [planner.py](../src/contextforge/core/planner.py), then
`validate_and_save_tasks` and `_new_tasks` in plan.py.

The controlled planner reply names tasks `t1`, `t2`, `t3`. Code validates the output shape,
task count, references, dependencies and cycles. The plan gate validates authorized inputs
and saves real Task records. It generates IDs and replaces `dependsOn: ["t1", "t2"]` with
the corresponding task IDs. The model does not select database IDs.

Compare the raw structured response, validated plan and stored tasks on screen. Identify
which fields came from the model and which code supplied. A schema-valid plan still requires
evaluation for coverage and usefulness.

## Checkpoint 4: task-specific context and the tool loop

Open `build_task_graph`, `call_model`, `run_tools`, `_build_model_request` and `_invoke_tool`
in [execute.py](../src/contextforge/worker/stages/execute.py).

For the first task, inspect:

1. The execution prompt differs from the planner prompt.
2. The task goal is included alongside the overall request; tool access follows task kind.
3. The assistant's search call contains a name, arguments and call ID.
4. Python checks the tool and shared budget, performs the operation and saves evidence.
5. The next call contains the assistant tool request and a result with the matching call ID.
6. A subsequent fetch adds source text; the model can then return a structured `TaskResult`.

Our executor constructs the next request using a freshly rendered context message followed
by the retained loop history. The model can observe only the data that reaches that request.
Explain the difference between search snippets and fetched page text using
[web.py](../src/contextforge/infra/web.py).

## Checkpoint 5: context engineering is a set of decisions

| Decision | Current implementation to inspect |
|---|---|
| Selection | Stage-specific `ContextInputs`; task goals, dependency summaries and allowed evidence |
| Placement | Stable instruction blocks; changing request data and execution history |
| Formatting | Tagged sections, structured tool definitions, typed result schemas and evidence references |
| Retention | Working facts are bounded; saved task results and source snapshots preserve more detail |
| Reduction | Old tool results can become stubs; longer history can require a summary model call |
| Trust | Retrieved text remains source data; backend checks permissions, ownership and budgets |

Open [compaction.py](../src/contextforge/core/context/compaction.py). The exported before/after
stubbing example is an explicit invocation on a copy with `keep_turns=0` to expose the
mechanism; it is not an automatic compaction event from this short task. Normal policy retains
recent turns and checks whether history fits its estimated budget. Token estimates are not
provider-reported token usage. Stable prefixes can support caching, but do not prove a cache hit.

An artifact reference preserves auditability and points to saved content. Do not imply that
every stored object is automatically available through a model tool. The current task tool
allowlist and data passed into context determine access. Checkpoints, application records,
the model's input context and long-term learned memory are distinct concerns.

## Checkpoint 6: task outcomes become a report

The outer workflow collects task outcomes and makes ready dependent tasks eligible to run.
It then performs citation checks, optional configured semantic support review, summary and
assessment review, and reviewed report composition with a findings fallback.

Open [citation_checks.py](../src/contextforge/worker/stages/citation_checks.py),
[review.py](../src/contextforge/worker/stages/review.py) and
[report.py](../src/contextforge/worker/stages/report.py). Explain:

- A matching quote is a provenance check; its existence does not establish the claim's meaning.
- A conclusion must retain all of its declared supporting findings.
- Missing/unavailable support reviews withhold claims; they do not silently certify them.
- A final writer introduces another opportunity for unsupported wording, so its paragraphs
  and headings need checking too.

The offline export intentionally stops after one task. Show the final report and its real
trace from a separately rehearsed live investigation. Use the existing trace API and
`GET /chats/{chat_id}/requests/{request_id}/model-calls/{model_call_id}/transcript` for real
recorded inputs/outputs; [API testing](api-testing.md) explains submission and capture. Full
transcripts are available only when recording was enabled and the stored objects remain.
Ordinary `backend.log` is for operational events, not a substitute for full prompt transcripts.

## Questions to ask the room

1. Which data belongs in every call, and which data belongs only in this stage?
2. What must survive if we remove a 10,000-character tool response from context?
3. What prevents a source page from expanding the tool's permissions?
4. What happens if one prerequisite fails but another task succeeds?
5. Which checks prove software behavior, and which require adjudicating the research output?

Finish with the original request, one executor call and the report side by side. Trace each
material conclusion back through retained findings to saved evidence, and identify each gap.
