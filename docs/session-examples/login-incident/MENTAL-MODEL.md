# Agent mental model — quick cheat sheet

**The LLM chooses a next action or returns a result. Python controls execution, storage and
what the next model call can see.**

```text
User request
  → Python assembles instructions + relevant context + tool schemas
  → LLM returns plan / tool request / structured result
  → Python validates it
  → if tool request: execute → save large data → return bounded evidence → call LLM again
  → if accepted stage result: save → advance dependent work → finish request when ready
```

## Five sentences to remember

1. **Input:** “This is everything the model can see on this call.”
2. **Output:** “This is what the model wants Python to do, or the result it proposes.”
3. **Execution:** “Python checks the request and actually calls the tool.”
4. **Storage:** “Full supplied rows stay in an artifact; a bounded preview enters context.”
5. **Next call:** “We send the relevant history and new evidence back to the model.”

## Common confusions

| Don't confuse | Remember |
|---|---|
| LLM tool call / tool result | A small request from the model can produce a huge response from a service |
| User Message / ModelCall | One user instruction can produce many model calls |
| Task completion / request completion | Other tasks, evidence checks or actions may remain |
| Artifact / context | Stored data is not automatically model-visible |
| Artifact ID / read permission | Python must authorize and resolve the reference |
| Shortening / summarizing | Current row/cell shaping is deterministic; LLM summarization is separate |
| NDJSON / JSON array | NDJSON is one independent JSON object per line |
| Table / storage format | A table can simply be a UI rendering of JSON rows |
| Checkpoint / memory / evidence | Execution recovery, selected context and saved sources have different roles |
| Retryable / automatically retried | A retry hint is not an executed retry or permission to retry forever |

Bedrock vocabulary: `system` = rules; `messages` = context/history; `toolConfig` = contracts;
`toolUse` = requested action; `toolUseId` = pairing ID; `toolResult` = returned evidence.
`return_result` is our structured-result convention. `stopReason: tool_use` alone does not
mean the task succeeded.

## What this project actually implements

Context assembly, model/tool loops for supported task kinds, structured-result validation,
row offloading, blob reads, compaction and evidence checks are actual code. The incident's
private MCP adapters, general `query_artifact`, automatic replanning and Jira action workflow
are proposed. Fixture data and LLM outputs are scripted. Learning the loop is real; these
examples do not prove that all proposed integrations already work.

Open code in this order (under `src/contextforge/`):

1. `worker/workflow.py` — request scheduling.
2. `worker/stages/plan.py` — planner context and plan validation.
3. `worker/stages/execute.py` — model → tool → model loop.
4. `core/context/tool_results.py` — full rows saved, preview shortened.
5. `infra/bedrock.py` — application messages become provider JSON.

**Rehearsal question:** “What changed between this model call and the previous one?”
If you can identify the new evidence, retained history, available tools and next Python action,
you understand the central mechanism.
