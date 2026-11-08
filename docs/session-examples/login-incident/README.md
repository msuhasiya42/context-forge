# Explain the agent one model call at a time

**For a single-file rehearsal, read [the complete spoken session](FULL-SESSION-REHEARSAL.md).** It includes narration, audience questions, failure cases and the full JSON appendix.

Open this guide and [SESSION.json](SESSION.json). The JSON now follows **five model calls**,
each with the same headings:

1. `say`: your explanation to the audience; not sent to the model.
2. `input_to_llm`: the complete example request Python sends.
3. `output_from_llm`: the scripted response in provider format.
4. `python_does`: what backend code does with that response.
5. The saved data or small result that becomes input to the next call.

**Read this once before presenting:** the model/tool/context cycle is real engineering, and
the inputs use our actual Bedrock formatter. Incident values and model replies are fictional.
Our row offloader actually saves the sample rows. Private MCP adapters, artifact-query tool
and Jira workflow are proposed. These JSON inputs are teaching requests, not captures from
an operational incident. Provider response bodies omit unmeasured usage/latency metadata.

## First, explain the few fields that repeat

| Field | Say this |
|---|---|
| `modelId` | “Which model we are calling.” |
| `system` | “The instructions for this stage.” |
| `messages` | “The user/task context and previous conversation with the tools.” |
| `inferenceConfig.maxTokens` | “The maximum generated output; not a limit on all saved data.” |
| `inferenceConfig.temperature` | “A sampling setting; zero does not guarantee correctness.” |
| `toolConfig.tools` | “The tools the model may request and their argument shapes.” |
| `inputSchema` | “The JSON structure Python expects for a tool's arguments.” |
| `toolUse` | “The model is asking Python to do something.” |
| `toolUseId` | “The ID connecting this tool request to its response.” |
| `toolResult` | “The result Python sends back for that ID.” |
| `return_result` | “Our convention for returning the final structured result of this stage.” |

`return_result` also appears as a tool in Bedrock, but Python treats it as a result to
validate—not an external API to call. `stopReason: tool_use` means the model handed control
back; inspect the tool name to distinguish another operation from stage completion.

Bedrock puts tool results inside a message whose role is `user`. That is its API encoding;
it does not mean a human typed the tool's result. At our application boundary, this is a
`ToolMessage`. Teach one provider format throughout rather than switching between shapes.

## Call 1 — “We received this request. Let's ask for a plan.”

**Open `cycles[0]`.** Read `say`, then expand the input.

> “Customers report login failures after a release. We need metrics, recent Jira issues,
> release notes, an evidence-backed summary and a ticket. Before calling the LLM, Python
> adds planning instructions, the service and time windows, and available capabilities.”

Point to `system`, then the user's request inside `messages`, then `return_result`'s schema.
The first call has no previous assistant message. The supplied service, release ID and time
windows are backend context; the model did not discover them from the short user question.

**Next, expand `output_from_llm`.** The response contains a plan under
`output.message.content[0].toolUse.input`:

- t1: read metrics.
- t2: search Jira.
- t3: search Drive, then read the matching document.
- t4: combine the three results.
- t5: prepare the requested Jira action.

**Then point to `python_does`.**

> “Python parses and validates the plan, checks dependencies and permissions, assigns stored
> task IDs and starts the independent read tasks. Returning a plan does not execute it.”

The planner capability descriptions are instructions about future work, not active research
tools in this call. Its only output mechanism here is `return_result`.

## Call 2 — “Let's follow the metrics task.”

**Open `cycles[1]`.** Show its complete input, then output.

> “This call has execution instructions, the metrics task and the tool definitions. The model
> doesn't know the measurements yet. It asks Python to run `query_prometheus`.”

Read only three things in the assistant response: `toolUseId`, `name`, `input`. Explain the
arguments: datasource identifies the metrics store; `expr` is PromQL; start/end define the
window; step controls sampling intervals. Metric names and datasource values are fictional
configured inputs. Tool schemas here are teaching subsets, not live discovery captures.

> “The model returns a small instruction. Python runs the tool. The **tool** returns the large
> data. These are two different responses.”

The full provider example is linked in `tool_response_file`. It is JSON containing time
series: labels plus timestamp/numeric-string pairs. The proposed adapter joins success and
failure series into 600 normalized rows. It must handle warnings and missing samples in a
real implementation; our values are controlled fixtures, not executed PromQL.

### Python saves the large part and builds the small part

Show `saved_artifact_metadata`, `saved_artifact_file`, then `small_result_for_next_call`.

> “The existing row processor writes all 600 rows to BlobStore as NDJSON: one JSON object
> per line, in a `.jsonl` object. The Artifact record holds the ID, ownership, format, size
> and location. It is metadata—not the full data.”

In this demo BlobStore is in-memory and we export the bytes to a local file. Deployment can
use S3 through the same boundary; no real upload occurred in this fixture.

> “The model gets only 50 rows and a notice saying the remaining rows are saved under this
> artifact ID. Long cells are also shortened in the preview; full supplied rows remain saved.”

This is deterministic Python shaping, not a separate LLM summarization call. The first 50
rows contain **baseline minutes only**, all at 1%. They cannot establish the post-release rate.
The full saved data includes the spike. Saving normalized rows preserves those rows, not the
original provider envelope; preserve that separately if exact provider replay is needed.

## Call 3 — “We pass the small result back. The model needs more data.”

**Open `cycles[2]`.** This is a new LLM call, with the previous exchange included.

Point to the growing `messages` list:

```text
user:      metrics task/context
assistant: query_prometheus, ID metrics-001
user:      toolResult for metrics-001, containing the shortened preview
```

> “The model sees that this is incomplete. It asks for `query_artifact`, using the saved
> artifact ID and grouping by window and region.”

There is no magic “needs more data” API field. The model expresses that decision through
another **tool request**. The artifact tool is our proposed application tool, not an MCP
standard. It still needs implementation and registration in this project.

> “Python checks access, reads all 600 saved rows and calculates the aggregates. It sends
> back only ten regional groups and two window totals, with provenance.”

Read `small_result_for_next_call.windowTotals`: baseline is 3,000 / 300,000 = 1%; after
release it is 23,400 / 300,000 = 7.8%. That is 6.8 percentage points. Python does the arithmetic.
Reading the full file in Python does not require putting the full file into the model prompt.

## Call 4 — “Now we have enough evidence to finish this task.”

**Open `cycles[3]`.** The full next input contains five messages: task, metrics request,
metrics preview, artifact query, aggregate response. Instructions and tool schemas remain.

> “This time the model receives the complete aggregates. It returns findings and limitations
> through `return_result`. Python validates and saves the result, then ends this task loop.”

Inspect the output: measured failure-rate increase, concentration in us-east-1, and a limitation
that timing does not prove cause. Ending the metrics task does not finish the whole request.

This is the core loop:

```text
Python builds context → LLM returns tool request → Python executes
→ Python saves/shapes result → result enters next LLM call
→ another tool request OR a validated stage result
```

Each call sees only its supplied context. Models do not automatically remember prior calls,
open S3 objects, or retrieve data because an artifact ID exists in a prompt.

## Call 5 — “Other read tasks finished. Let's combine the evidence.”

**Open `cycles[4]`.** The input includes metrics findings plus completed Jira and Drive task
results. Their repetitive model loops are not expanded; `supporting_source_tasks` preserves
the example Drive calls/results and normalized Jira result.

> “The report call combines measured facts, a Jira hypothesis and a documented release
> change. It prepares a summary and concrete Jira parameters. It must not turn correlation
> into proof of root cause.”

Python validates the proposed action, source references, permissions, duplicates and required
fields. It resolves evidence into usable links/attachments, executes only when checks pass,
and saves the confirmed outcome. The user already requested creation; additional confirmation
depends on configured policy. The final fixture shows a simulated success, not a real ticket.

Do not claim a final answer must require yet another LLM call: Python can render the validated
summary plus the tool-confirmed ticket outcome. The full incident action pipeline is proposed.

## How to run the 60-minute session

| Minutes | Show |
|---|---|
| 0–8 | Request, repeated field names and implemented/proposed distinction |
| 8–18 | Call 1: planner input, output, Python validation |
| 18–30 | Call 2: tool request versus large tool response; artifact versus preview |
| 30–40 | Call 3: incomplete preview → read-back request → aggregates |
| 40–47 | Call 4: growing context → completed metrics result |
| 47–53 | Call 5: combine evidence, validate action, final answer |
| 53–60 | Failure examples and audience questions |

## Keep nearby

- [Mental model and cheat sheet](MENTAL-MODEL.md): one quick page to rehearse.
- [Failure cases](FAILURES.md): unavailable tools, errors, retries and next model turns.
- [Technical explanations](reference/EXPLANATIONS.md): formats, source caveats and code map.
- [Sources](SOURCES.md): official provider documentation.

Regenerate fixtures and the narrated JSON without external calls:

```sh
.venv/bin/python -m scripts.export_incident_artifact_demo
.venv/bin/python -m scripts.export_provider_response_examples
.venv/bin/python -m scripts.bundle_session_walkthrough
```
