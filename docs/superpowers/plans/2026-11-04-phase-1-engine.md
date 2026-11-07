# Phase 1: Engine, local — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One request runs end to end on a scripted model and a fixture dataset through a LangGraph graph, survives a process restart, pauses for a clarification and resumes, and cannot be started twice by duplicate delivery.

**Architecture:** A `core` package holds Pydantic entities, repositories (interface plus in-memory), the model boundary (interface plus scripted fake plus a recorder that writes ModelCall records), the Context Builder, the Tool Result Processor and compaction. A `worker` package holds the request workflow written with the LangGraph Functional API: one `@entrypoint` that calls the plan stage, pauses with `interrupt()` for a clarification, validates the plan in code, runs tasks as `@task` calls in dependency-driven waves (five in flight), then cite and report; the loop inside one task (model, tool, model) is a small Graph API StateGraph with its own `messages`. A runner owns status transitions, start, resume and restart. No API, no UI, no AWS.

**Tech Stack:** Python 3.12, `uv`, Pydantic 2, LangGraph 1.x with `langgraph-checkpoint-sqlite`, `langchain-core` message types, DuckDB (in-process, over the fixture CSV only), PyYAML, `python-ulid`, pytest, ruff, mypy strict.

**Spec:** `docs/implementation-plan.md` (sections 1 to 6, 8, 9, 10, 12, 13) and `docs/system-design.md` (sections 6, 8, 13 grill rows, 14). Read both before any task.

## Global Constraints

- Python 3.12; `uv` with a committed `uv.lock`; `ruff` and `mypy --strict` clean; `pytest` green before every commit.
- Code lives under `src/contextforge/{core,worker,api}`; `api` stays empty in phase 1. (The spec names the packages `core`, `worker`, `api`; nesting them under one distribution package avoids shadowing generic names.)
- Naming follows implementation-plan 12.1: snake_case Python, camelCase on the wire and in YAML, enum values and error codes exactly as the spec writes them. Field names are camelCase in Pydantic models via `alias_generator=to_camel` with `populate_by_name=True`; Python attributes are snake_case; code constructs models by attribute name. `extra="forbid"` on every entity. `from` is never a field name; the sender field is `sender`.
- Ids are ULID strings; timestamps are ISO 8601 UTC strings produced by one helper.
- Enumerations are `StrEnum`s whose values are exactly the spec's strings (`ACCEPTED`, `sql_analysis`, …).
- No code outside `core/repositories` and `core/storage` builds a storage key or path.
- Compaction configuration: cap 40,000 input tokens, response allowance 4,000, margin 10 percent, keep last 6 messages; all read from `config/budgets.yaml`, never hard-coded in logic.
- Tool Result Processor row cap: 50. Facts cap in the brief: 30 entries.
- A planner result has 2 to 6 tasks; task kinds are exactly `sql_analysis`, `pdf_lookup`, `source_search`, `reconcile`.
- Tests before implementation for every task. Commit after every task.
- No paid model calls, no network, no AWS in this phase.
- Tool results are results even when they fail: `ToolResult` carries `status: ok | error | not_executed`, `error`, `error_class: code | system`, `retryable`; the processor turns every one into a ToolMessage. Every result text starts "showing N of M rows"; zero rows is a valid outcome.
- Loop control inside a task (7 Nov 2026): at the last allowed tool call a labelled user turn asks for the structured result, never a forced tool choice; a repeated tool call with the same tool and canonical arguments returns the existing ToolMessage and spends no slot; `noProgressTurns` consecutive model turns without a new fact or artifact end the task with what exists.
- Byte stability: `build_context` is a pure function; the same inputs give identical bytes, sections 1 and 2 never contain dates or ids; stubbing the same transcript twice yields identical stubs. Both are tested.
- `Usage` carries `cache_read_tokens`, `cache_write_tokens`, `thinking_tokens` (all optional); every ModelCall records `price_version` from `config/models.yaml` and `stop_reason`.
- The planner is retried once with its validation errors appended as a constraint before `PlanFailed`.
- Request orchestration uses the LangGraph Functional API (`@entrypoint`, `@task`); the per-task model loop uses the Graph API. On resume the entrypoint re-runs from its first line and completed task results are restored by task name and call order, so: every model call and every repository write lives inside a `@task`; the entrypoint body is deterministic given restored results (futures created and awaited in `task_id` order, no clock, no randomness); `interrupt()` is called only in the entrypoint, never inside a task.
- Writes that a re-run can repeat are idempotent: raw_result artifact ids derive from `(task_id, tool_call_id)`, clarification Message ids from `(request_message_id, attempt)`, the report Message id from the request; the plan gate and the execute stage return existing records instead of creating or re-running when the repository already shows the outcome.
- Limits from the 5 November grill, all read from `config/budgets.yaml`: 50 model calls per request, 5 tasks in flight, 6 tool calls per task, 1 automatic retry per task, then FAILED. A request is FAILED only when the plan fails or no task completes; otherwise the report lists failed and skipped tasks under uncertainties. At most two clarifications, then the planner is told to plan with stated assumptions.
- Cooperative cancellation: before every model call and before every wave of tasks, stages read the request status; on CANCELLED the workflow stops and returns `{"status": "CANCELLED"}`.
- SSE and the API read Messages and Tasks only; nothing in `RequestState` is shown to the user.
- Every log line carries `request_message_id` and, when inside a task, `task_id`, via a context variable in `core/logging.py`.

## Review Focus

1. A clarification reply whose `inReplyToMessageId` is not the pending interrupt: the runner must refuse it, save nothing, and leave the request WAITING_USER (Task 10).
2. A plan naming an artifact from another chat: the plan gate must fail the request with a user-facing reason and create no Task records (Task 8).
3. Exactly 50 rows must not offload; 51 rows must offload and the stub must say "1 more row" (Task 7).
4. Compaction keep-boundary landing between a tool call and its result: the boundary must move earlier so both stay verbatim (Task 11).
5. The scripted model running out of responses or returning output that fails the schema: the request ends FAILED with a reason, the ModelCall records outcome ERROR, and resume does not loop (Task 9).
6. A process crash after two tasks completed: the requeued run must not re-execute them or write a second ModelCall for them, must write exactly one report, and must not create a second raw_result artifact for a replayed tool call (Task 10).
7. A second clarification after the first was answered: the second interrupt must receive the second answer, not the first, and exactly two clarification Messages exist (Task 10).
8. A tool that raises: the model must see an error result with a class and the task must continue, not crash the wave (Task 9).

---

## File structure

```
pyproject.toml                      project, deps, ruff, mypy, pytest config
uv.lock
config/budgets.yaml                 compaction numbers, call budget, tasks in flight, tool calls per task, retries
config/models.yaml                  stage -> model id, effort, max tokens (scripted in phase 1)
prompts/agent-memory.md             v1 text from spec 10.1
prompts/plan.1.md                   spec 10.2
prompts/execute.1.md                spec 10.3
prompts/compress.1.md               spec 10.4
fixtures/transactions_sep_oct.csv   60 rows (Task 5)
fixtures/request.txt                the spending request text
src/contextforge/__init__.py
src/contextforge/core/ids.py                  new_ulid(), now_iso()
src/contextforge/core/values.py               Locator, EvidenceRef, ArtifactRef, Usage
src/contextforge/core/entities/user.py        User
src/contextforge/core/entities/chat.py        Chat
src/contextforge/core/entities/message.py     seven Message classes, Message union, enums
src/contextforge/core/entities/task.py        Task, TaskResult, TaskKind, TaskStatus, validate_structured_data
src/contextforge/core/entities/model_call.py  ModelCall, Stage, Outcome
src/contextforge/core/entities/artifact.py    Artifact, ArtifactKind, ArtifactStatus, IndexStatus
src/contextforge/core/entities/integration.py Integration, IntegrationConnection, IntegrationTool
src/contextforge/core/planner.py              PlannerOutput, PlannedTask
src/contextforge/core/state.py                RequestState, TaskState, PlanEntry, Fact, OpenQuestion, PendingInterrupt
src/contextforge/core/repositories/base.py    Protocols + StaleStatusError
src/contextforge/core/repositories/memory.py  in-memory implementations
src/contextforge/core/storage.py              BlobStore Protocol + MemoryBlobStore
src/contextforge/core/queue.py                Queue Protocol + InProcessQueue (resume and start messages)
src/contextforge/core/logging.py              request/task context vars, JSON log formatter
src/contextforge/core/tools/base.py           Tool Protocol, ToolContext, ToolResult
src/contextforge/core/models/base.py          ModelRequest, ModelResponse, ToolCall, ToolSpec, ModelClient
src/contextforge/core/models/scripted.py      ScriptedModel, ScriptExhausted
src/contextforge/core/models/recording.py     RecordingModelClient
src/contextforge/core/context/tokens.py       estimate_tokens
src/contextforge/core/context/budgets.py      Budgets, load_budgets
src/contextforge/core/context/prompts.py      load_prompt, prompt_version
src/contextforge/core/context/builder.py      ContextInputs, BuiltContext, build_context
src/contextforge/core/context/tool_results.py ProcessedRows, process_rows
src/contextforge/core/context/compaction.py   stub_old_tool_results, summarize_messages, compact
src/contextforge/core/services/submit.py      submit_request
src/contextforge/worker/tools/sql_query.py    SqlQueryTool implementing Tool over the fixture CSV
src/contextforge/worker/dependencies.py               WorkerDependencies dataclass, task semaphore
src/contextforge/worker/stages/guards.py      Cancelled, check_not_cancelled, CallBudgetExceeded, check_call_budget
src/contextforge/worker/stages/plan.py        create_research_plan, validate_and_save_tasks, PlanFailed, clarification_id
src/contextforge/worker/stages/execute.py     build_task_graph (Graph API loop), execute_task
src/contextforge/worker/stages/stubs.py       reconcile_stub, skipped_reason
src/contextforge/worker/stages/citation_checks.py        cite_stage stub: passes facts through unchanged in phase 1
src/contextforge/worker/stages/report.py      write_report
src/contextforge/worker/workflow.py           build_research_workflow: the @entrypoint run_request wrapping each stage as a @task
src/contextforge/worker/runner.py             RequestRunner: start, resume, pending_interrupt
tests/                                        one test module per task, named below
```

---

### Task 1: Project skeleton

**Files:**
- Create: `pyproject.toml`, `.python-version`, `src/contextforge/__init__.py`, `src/contextforge/core/__init__.py`, `src/contextforge/worker/__init__.py`, `src/contextforge/api/__init__.py`, `prompts/agent-memory.md`, `prompts/plan.1.md`, `prompts/execute.1.md`, `prompts/compress.1.md`, `config/budgets.yaml`
- Test: `tests/test_skeleton.py`

**Interfaces:**
- Produces: importable package `contextforge`; prompt files whose text is exactly spec sections 10.1 to 10.4; `config/budgets.yaml` with keys `capInputTokens: 40000`, `responseAllowanceTokens: 4000`, `marginFraction: 0.10`, `keepLastMessages: 6`, `toolResultRowCap: 50`, `factsCap: 30`, `callBudget: 50`, `tasksInFlight: 5`, `toolCallsPerTask: 6`, `taskRetries: 1`, `noProgressTurns: 3`, `columnCap: 20`, `cellCharCap: 200`; `config/models.yaml` with a top-level `priceVersion: 2026-11-04` and prices per stage model, mapping each stage to `{ model, effort, maxTokens }` with `model: scripted` for every stage in phase 1 and the Bedrock ids in comments (`anthropic.claude-opus-5-5` plan and cite, `anthropic.claude-haiku-5-5` execute and compress).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skeleton.py
from pathlib import Path
import yaml

def test_package_imports() -> None:
    import contextforge  # noqa: F401

def test_prompt_files_exist_and_are_nonempty() -> None:
    for name in ["agent-memory.md", "plan.1.md", "execute.1.md", "compress.1.md"]:
        assert Path("prompts", name).read_text().strip()

def test_budgets_yaml_has_spec_values() -> None:
    b = yaml.safe_load(Path("config/budgets.yaml").read_text())
    assert b == {"capInputTokens": 40000, "responseAllowanceTokens": 4000,
                 "marginFraction": 0.10, "keepLastMessages": 6,
                 "toolResultRowCap": 50, "factsCap": 30, "callBudget": 50, "tasksInFlight": 5,
                 "toolCallsPerTask": 6, "taskRetries": 1, "noProgressTurns": 3, "columnCap": 20, "cellCharCap": 200}

def test_models_yaml_covers_every_stage() -> None:
    m = yaml.safe_load(Path("config/models.yaml").read_text())
    assert set(m) >= {"plan", "execute", "cite", "compress"}
    assert all(v["model"] == "scripted" for v in m.values())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skeleton.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge` (after `uv init` and `uv add` below, the first run fails on the missing package or files).

- [ ] **Step 3: Create the project**

`pyproject.toml`: name `contextforge`, `requires-python = ">=3.12"`, build backend `hatchling`, packages `src/contextforge`. Dependencies: `pydantic>=2.9`, `langgraph>=1.0`, `langgraph-checkpoint-sqlite>=2.0`, `langchain-core>=0.3`, `duckdb>=1.1`, `pyyaml>=6`, `python-ulid>=3`. Dev group: `pytest>=8`, `ruff>=0.7`, `mypy>=1.13`, `types-PyYAML`. Ruff: line length 100, rules `E,F,I,B,UP`. Mypy: `strict = true`, `plugins = ["pydantic.mypy"]`, `mypy_path = "src"`. Pytest: `testpaths = ["tests"]`, `pythonpath = ["src"]`. `.python-version` is `3.12`. Run `uv lock`.

Write the four prompt files with the exact text of spec 10.1 to 10.4 (no headings added). Write `config/budgets.yaml` with the thirteen keys above.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -v && uv run ruff check . && uv run mypy src`
Expected: 4 passed; ruff and mypy report no issues.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock .python-version src prompts config tests/test_skeleton.py
git commit -m "feat: project skeleton with prompts and budgets config"
```

---

### Task 2: Value types, ids and entities

**Files:**
- Create: `src/contextforge/core/ids.py`, `src/contextforge/core/values.py`, `src/contextforge/core/entities/__init__.py`, `user.py`, `chat.py`, `message.py`, `task.py`, `model_call.py`, `artifact.py`, `integration.py` (all under `entities/`)
- Test: `tests/test_entities.py`

**Interfaces:**
- Produces:
  - `ids.new_ulid() -> str`, `ids.now_iso() -> str`.
  - `values.Locator(kind: LocatorKind, value: str)`, `values.EvidenceRef(artifact_id, locator, excerpt: str | None = None)`, `values.ArtifactRef(artifact_id, role: Literal["input","output"])`, `values.Usage(input_tokens: int, output_tokens: int, cache_read_tokens: int | None = None, cache_write_tokens: int | None = None, thinking_tokens: int | None = None)`.
  - A shared base `Entity(BaseModel)` with `model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")`.
  - `message.py`: `Sender`, `MessageKind`, `RequestStatus`, `ActionStatus`, `Decision` enums; classes `RequestMessage`, `ClarificationMessage`, `ClarificationResponseMessage`, `ProposedActionMessage`, `ActionResponseMessage`, `ReportMessage`, `TextMessage`, each with `kind: Literal[...]`; `Message = Annotated[Union[...], Field(discriminator="kind")]`; `MessageAdapter = TypeAdapter(Message)`; `ReportBody(claims: list[Claim], inputs_consulted: list[ArtifactRef], deliverables: list[ArtifactRef] = [], uncertainties: list[str], coverage: dict[str, Any])`, `Claim(claim_id, text, evidence_refs)`; `ActionPayload(tool, arguments: dict[str, Any], account: str | None)`.
  - `task.py`: `TaskKind`, `TaskStatus`, `Task`, `TaskResult(summary, facts: list[Fact] = [] (max six, each `{text, evidence_refs}`), structured_data: dict[str, Any], evidence_refs, artifact_refs, uncertainties, coverage: dict[str, Any])`, `validate_structured_data(kind: TaskKind, data: dict[str, Any]) -> None` raising `ValueError` when any list value has more than 50 entries (the "never row sets" rule).
  - `model_call.py`: `Stage`, `Outcome`, `ModelCall` with `price_version: str`, `stop_reason: Literal["end_turn","tool_use","max_tokens"] | None = None`, `tool_set_hash: str | None = None` beside the spec 3.5 fields.
  - `artifact.py`: `ArtifactKind`, `ArtifactStatus`, `IndexStatus`, `Artifact`.
  - `chat.py`: `Chat` with `last_request_status: RequestStatus | None = None`. `user.py`: `User` with `instructions: str | None` capped at 2,000 characters. `integration.py`: the three integration entities as in spec 3.7 to 3.9.

Field lists are spec section 3 verbatim, including required-ness and defaults; this task adds nothing and drops nothing.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_entities.py
import pytest
from pydantic import ValidationError
from contextforge.core.entities.message import MessageAdapter, RequestMessage, ActionResponseMessage
from contextforge.core.entities.task import TaskKind, validate_structured_data
from contextforge.core.entities.user import User
from contextforge.core.ids import new_ulid

def test_request_message_round_trips_camel_case() -> None:
    raw = {"messageId": "m1", "chatId": "c1", "userId": "u1", "sender": "user", "kind": "request",
           "content": "Compare", "status": "ACCEPTED", "failureReason": None,
           "createdAt": "2026-11-04T10:00:00Z", "updatedAt": "2026-11-04T10:00:00Z"}
    m = MessageAdapter.validate_python(raw)
    assert isinstance(m, RequestMessage)
    assert m.model_dump(by_alias=True) == raw

def test_message_without_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MessageAdapter.validate_python({"messageId": "m1", "chatId": "c1", "userId": "u1",
                                        "sender": "user", "content": "x",
                                        "createdAt": "t", "updatedAt": "t"})

def test_message_with_from_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        RequestMessage.model_validate({"messageId": "m1", "chatId": "c1", "userId": "u1",
            "from": "user", "sender": "user", "kind": "request", "content": "x",
            "status": "ACCEPTED", "createdAt": "t", "updatedAt": "t"})

def test_action_response_requires_decision() -> None:
    with pytest.raises(ValidationError):
        ActionResponseMessage.model_validate({"messageId": "m7", "chatId": "c1", "userId": "u1",
            "sender": "user", "kind": "action_response", "content": "ok", "requestMessageId": "m1",
            "inReplyToMessageId": "m6", "createdAt": "t", "updatedAt": "t"})

def test_structured_data_rejects_row_sets() -> None:
    with pytest.raises(ValueError):
        validate_structured_data(TaskKind.SQL_ANALYSIS, {"rows": [{"i": i} for i in range(51)]})
    validate_structured_data(TaskKind.SQL_ANALYSIS, {"pairs": [{"i": i} for i in range(50)]})

def test_user_instructions_capped() -> None:
    with pytest.raises(ValidationError):
        User(user_id="u1", email="a@b", status="ACTIVE", created_at="t", updated_at="t",
             instructions="x" * 2001)

def test_ulids_sort_by_creation() -> None:
    a, b = new_ulid(), new_ulid()
    assert a < b and len(a) == 26
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_entities.py -v`
Expected: FAIL with `ModuleNotFoundError` for `contextforge.core.entities`.

- [ ] **Step 3: Implement the modules listed in Interfaces**

One file per entity. Enum values are the spec strings. `MessageKind` values: `request, clarification, clarification_response, proposed_action, action_response, report, text`. `RequestStatus`: `ACCEPTED, RUNNING, WAITING_USER, COMPLETED, FAILED, CANCELLED`. `ActionStatus`: `PROPOSED, APPROVED, REJECTED, EXPIRED, EXECUTING, EXECUTED, FAILED, UNCERTAIN`. `Decision`: `approve, reject`. `TaskStatus`: `PLANNED, RUNNING, COMPLETED, FAILED, SKIPPED, CANCELLED`. `Stage`: `understand, clarify, plan, execute, summarize, evaluate, cite, compress, tool_select`. `Outcome`: `COMPLETED, ERROR, TIMEOUT, CANCELLED`. `ArtifactKind`: `upload, dataset, raw_result, deliverable`; `ArtifactStatus`: `UPLOADING, AVAILABLE, FAILED, DELETED`; `IndexStatus`: `NOT_INDEXED, PENDING, INDEXED, FAILED`. `new_ulid` uses `ulid.ULID()`; `now_iso` is UTC with a `Z` suffix and second precision.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_entities.py -v && uv run mypy src && uv run ruff check .`
Expected: 7 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core tests/test_entities.py
git commit -m "feat: value types and the nine entities with Message union"
```

---

### Task 3: Planner output and graph state

**Files:**
- Create: `src/contextforge/core/planner.py`, `src/contextforge/core/state.py`
- Test: `tests/test_planner_state.py`

**Interfaces:**
- Produces:
  - `planner.PlannedTask(ref: str, kind: TaskKind, goal: str, input_artifact_ids: list[str], depends_on: list[str] = [])`.
  - `planner.PlannerOutput(kind: Literal["plan","clarification"], goal, constraints: list[str], open_questions: list[str], question: str | None = None, tasks: list[PlannedTask] | None = None, next_action: str)` with a model validator enforcing: clarification requires `question`; plan requires 2 to 6 tasks, unique refs, every `depends_on` entry is another task's ref, no self-dependency.
  - `state.PlanEntry(TypedDict): task_id, kind, status`; `state.Fact: text, evidence_refs: list[dict]`; `state.OpenQuestion: text, resolved_by: str | None`; `state.PendingInterrupt: kind, message_id, question, next_action` (the payload passed to `interrupt()`; what the runner shows as pending).
  - `state.RequestState(TypedDict)`: `request_message_id: str`, `chat_id: str`, `user_id: str`, `goal: str`, `constraints: list[str]`, `plan: list[PlanEntry]`, `facts: list[Fact]`, `open_questions: list[OpenQuestion]`, `next_action: str`. A plain dict passed between workflow tasks and checkpointed with their results; it carries no transcript. (`chat_id` and `user_id` are carried so stages can write Messages and Artifacts without a lookup; they are identity, not working memory.)
  - `state.TaskState(TypedDict)`: `task_id`, `request_message_id`, `chat_id`, `user_id`, `constraints`, `dependency_summaries: list[str]`, `messages: Annotated[list[AnyMessage], add_messages]`, `result: dict | None`, `error: str | None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_planner_state.py
import pytest
from pydantic import ValidationError
from contextforge.core.planner import PlannerOutput, PlannedTask

def _task(ref: str, worker_dependencies: list[str] | None = None) -> PlannedTask:
    return PlannedTask(ref=ref, kind="sql_analysis", goal="g", input_artifact_ids=["a2"], depends_on=worker_dependencies or [])

def test_plan_requires_two_to_six_tasks() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(kind="plan", goal="g", constraints=[], open_questions=[], tasks=[_task("t1")], next_action="n")
    with pytest.raises(ValidationError):
        PlannerOutput(kind="plan", goal="g", constraints=[], open_questions=[],
                      tasks=[_task(f"t{i}") for i in range(7)], next_action="n")

def test_plan_dependency_must_reference_a_task() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(kind="plan", goal="g", constraints=[], open_questions=[],
                      tasks=[_task("t1"), _task("t2", ["t9"])], next_action="n")

def test_clarification_requires_question() -> None:
    with pytest.raises(ValidationError):
        PlannerOutput(kind="clarification", goal="g", constraints=[], open_questions=[], next_action="n")
    PlannerOutput(kind="clarification", goal="g", constraints=[], open_questions=[], question="Which?", next_action="n")

def test_spec_example_plan_validates() -> None:
    out = PlannerOutput.model_validate({"kind": "plan", "goal": "g", "constraints": ["c"], "openQuestions": [],
        "tasks": [{"ref": "t1", "kind": "sql_analysis", "goal": "g", "inputArtifactIds": ["a2"], "dependsOn": []},
                  {"ref": "t2", "kind": "reconcile", "goal": "g", "inputArtifactIds": [], "dependsOn": ["t1"]}],
        "nextAction": "run"})
    assert out.tasks and out.tasks[1].depends_on == ["t1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_planner_state.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.planner`.

- [ ] **Step 3: Implement `planner.py` and `state.py`**

`PlannerOutput` uses the same `Entity` base (camelCase aliases). `state.py` imports `AnyMessage` and `add_messages` from `langgraph.graph.message` for `TaskState` only.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_planner_state.py -v && uv run mypy src`
Expected: 4 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/planner.py src/contextforge/core/state.py tests/test_planner_state.py
git commit -m "feat: PlannerOutput schema and RequestState request state"
```

---

### Task 4: Repositories, idempotent submit and blob store

**Files:**
- Create: `src/contextforge/core/repositories/__init__.py`, `base.py`, `memory.py`; `src/contextforge/core/storage.py`; `src/contextforge/core/services/__init__.py`, `submit.py`
- Test: `tests/test_repositories.py`

**Interfaces:**
- Produces (all in `repositories/base.py` as `Protocol`s):
  - `class StaleStatusError(Exception)` with attributes `expected`, `actual`.
  - `MessageRepository`: `save(message: Message) -> None`; `get(chat_id, message_id) -> Message | None`; `list_by_chat(chat_id) -> list[Message]` ordered by `message_id`; `update_request_status(chat_id, message_id, expected: RequestStatus, new: RequestStatus, failure_reason: str | None = None) -> RequestMessage` raising `StaleStatusError`; `create_request(client_key: str, message: RequestMessage) -> RequestMessage` which returns the previously created message when `client_key` was seen before and stores nothing new; `pending_for_request(chat_id, request_message_id) -> list[Message]` returning clarification and proposed_action messages not yet answered.
  - `TaskRepository`: `save(task)`, `get(request_message_id, task_id)`, `list_by_request(request_message_id) -> list[Task]` ordered by `task_id`, `update_status(request_message_id, task_id, expected: TaskStatus, new: TaskStatus, *, result: TaskResult | None = None, failure_reason: str | None = None) -> Task` raising `StaleStatusError`.
  - `ModelCallRepository`: `save(call)`, `list_by_request(request_message_id)`, `sum_usage(request_message_id) -> Usage | None` (None when every call's usage is None; otherwise sums the known ones).
  - `ArtifactRepository`: `save(artifact)`, `get(chat_id, artifact_id)`, `list_by_chat(chat_id)`, `update_status(chat_id, artifact_id, expected, new)`.
  - `ChatRepository`: `save`, `get(user_id, chat_id)`, `list_by_user(user_id)` newest `updated_at` first.
  - `storage.BlobStore` Protocol: `put(key: str, data: bytes) -> str` returning the `storage_ref` (`mem://<key>`), `get(ref: str) -> bytes`; `MemoryBlobStore`.
  - `queue.WorkMessage(kind: Literal["start","resume"], chat_id: str, message_id: str, reply_message_id: str | None = None)`; `queue.Queue` Protocol: `send(msg: WorkMessage) -> None`, `receive() -> WorkMessage | None`; `queue.InProcessQueue` backed by `collections.deque`.
  - `logging.request_context(request_message_id: str, task_id: str | None = None)` context manager setting context vars; `logging.configure()` installing a JSON formatter that includes them on every record.
  - `memory.py`: `InMemoryMessages`, `InMemoryTasks`, `InMemoryModelCalls`, `InMemoryArtifacts`, `InMemoryChats` implementing the above with plain dicts; `Repositories` dataclass bundling the five.
  - `services/submit.py`: `submit_request(repos: Repositories, *, client_key: str, chat_id: str, user_id: str, content: str) -> RequestMessage` which builds a `RequestMessage` at `ACCEPTED` with a new ULID and calls `create_request`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_repositories.py
import pytest
from contextforge.core.repositories.memory import Repositories
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.services.submit import submit_request
from contextforge.core.entities.message import RequestStatus
from contextforge.core.entities.task import Task, TaskStatus
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.values import Usage

def test_submit_is_idempotent_on_client_key() -> None:
    repositories = Repositories.in_memory()
    a = submit_request(repositories, client_key="k1", chat_id="c1", user_id="u1", content="x")
    b = submit_request(repositories, client_key="k1", chat_id="c1", user_id="u1", content="x")
    assert a.message_id == b.message_id
    assert len(repositories.messages.list_by_chat("c1")) == 1

def test_conditional_transition_rejects_stale_expected() -> None:
    repositories = Repositories.in_memory()
    m = submit_request(repositories, client_key="k", chat_id="c1", user_id="u1", content="x")
    repositories.messages.update_request_status("c1", m.message_id, RequestStatus.ACCEPTED, RequestStatus.RUNNING)
    with pytest.raises(StaleStatusError):
        repositories.messages.update_request_status("c1", m.message_id, RequestStatus.ACCEPTED, RequestStatus.RUNNING)

def test_lists_are_in_ulid_order() -> None:
    repositories = Repositories.in_memory()
    ids = [submit_request(repositories, client_key=f"k{i}", chat_id="c1", user_id="u1", content="x").message_id for i in range(3)]
    assert [m.message_id for m in repositories.messages.list_by_chat("c1")] == sorted(ids)

def test_queue_round_trip_and_empty() -> None:
    from contextforge.core.queue import InProcessQueue, WorkMessage
    q = InProcessQueue()
    assert q.receive() is None
    q.send(WorkMessage(kind="start", chat_id="c1", message_id="m1"))
    assert q.receive() == WorkMessage(kind="start", chat_id="c1", message_id="m1")

def test_sum_usage_is_none_when_unknown() -> None:
    repositories = Repositories.in_memory()
    repositories.model_calls.save(ModelCall(model_call_id="k1", request_message_id="m1", stage="plan", model="fake",
        context_version="mem:1|prompt:plan.1", usage=None, outcome="COMPLETED", started_at="t"))
    assert repositories.model_calls.sum_usage("m1") is None
    repositories.model_calls.save(ModelCall(model_call_id="k2", request_message_id="m1", stage="execute", model="fake",
        context_version="mem:1|prompt:execute.1", usage=Usage(input_tokens=10, output_tokens=5),
        outcome="COMPLETED", started_at="t"))
    assert repositories.model_calls.sum_usage("m1") == Usage(input_tokens=10, output_tokens=5)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_repositories.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.repositories`.

- [ ] **Step 3: Implement the Protocols, in-memory classes, blob store and submit service**

`Repositories.in_memory()` is a classmethod returning a bundle with fresh instances. Status updates copy the model with `model_copy(update=...)` and set `updated_at = now_iso()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_repositories.py -v && uv run mypy src`
Expected: 5 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/repositories src/contextforge/core/storage.py src/contextforge/core/queue.py src/contextforge/core/logging.py src/contextforge/core/services tests/test_repositories.py
git commit -m "feat: repository interfaces, in-memory store, idempotent submit"
```

---

### Task 5: Fixture dataset and request

**Files:**
- Create: `fixtures/transactions_sep_oct.csv`, `fixtures/request.txt`, `tests/conftest.py`
- Test: `tests/test_fixture.py`

**Interfaces:**
- Produces: a CSV with header `row_id,txn_date,amount,direction,merchant,account_id,category,description`, 60 data rows, `row_id` 1 to 60, dates between 2026-09-01 and 2026-10-31, `direction` in `DEBIT|CREDIT`, four accounts `ACC-SAV-1`, `ACC-CUR-2`, `ACC-CARD-HDFC`, `ACC-CARD-ICICI`. Planted: transfer pair rows 12/13 (25,000 DEBIT from ACC-SAV-1 on 2026-09-03, CREDIT to ACC-CUR-2 on 2026-09-04, description "Transfer to current"/"Transfer from savings"), transfer pair rows 40/41 (15,000, 2026-10-02/2026-10-02), card settlement pair rows 50/51 (4,312 DEBIT from ACC-CUR-2 "HDFC card bill payment" 2026-10-09, CREDIT to ACC-CARD-HDFC "Payment received" 2026-10-09). Remaining rows are ordinary debits across categories `groceries, dining, transport, subscriptions, utilities, shopping` with at least one merchant (`Netflix`) in both months at a higher October amount. `fixtures/request.txt` holds the spec 9.1 request text.
- `tests/conftest.py` fixture `fixture_csv() -> Path` and `fixture_request() -> str`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fixture.py
import csv
from pathlib import Path

def test_fixture_shape_and_planted_pairs(fixture_csv: Path) -> None:
    rows = list(csv.DictReader(fixture_csv.open()))
    assert len(rows) == 60
    by_id = {int(r["row_id"]): r for r in rows}
    assert by_id[12]["amount"] == by_id[13]["amount"] == "25000.00"
    assert {by_id[12]["direction"], by_id[13]["direction"]} == {"DEBIT", "CREDIT"}
    assert by_id[40]["amount"] == by_id[41]["amount"] == "15000.00"
    assert by_id[50]["description"] == "HDFC card bill payment"
    assert by_id[51]["account_id"] == "ACC-CARD-HDFC"
    netflix = [r for r in rows if r["merchant"] == "Netflix"]
    assert {r["txn_date"][:7] for r in netflix} == {"2026-09", "2026-10"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_fixture.py -v`
Expected: FAIL, fixture `fixture_csv` not found.

- [ ] **Step 3: Write the CSV, the request text and `conftest.py`**

Generate the CSV with a short throwaway script and commit the CSV, not the script. Amounts have two decimals.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_fixture.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add fixtures tests/conftest.py tests/test_fixture.py
git commit -m "feat: 60-row fixture with planted transfers and card settlement"
```

---

### Task 6: Model boundary, scripted model, recording client

**Files:**
- Create: `src/contextforge/core/models/__init__.py`, `base.py`, `scripted.py`, `recording.py`; `src/contextforge/core/context/__init__.py`, `prompts.py`; `src/contextforge/core/tools/__init__.py`, `base.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces:
  - `base.ToolSpec(name: str, description: str, input_schema: dict[str, Any], strict: bool = True)`.
  - `base.ToolCall(id: str, name: str, arguments: dict[str, Any])`.
  - `base.ModelRequest(stage: Stage, model: str, system: list[str], messages: list[AnyMessage], tools: list[ToolSpec] = [], output_schema: type[BaseModel] | None = None, max_tokens: int = 4000, task_id: str | None = None)`.
  - `base.ModelResponse(text: str | None = None, structured: dict[str, Any] | None = None, tool_calls: list[ToolCall] = [], usage: Usage | None = None, stop_reason: Literal["end_turn","tool_use","max_tokens"] = "end_turn", replayed: bool = False)`.
  - `base.ModelClient` Protocol: `invoke(request: ModelRequest) -> ModelResponse`.
  - `scripted.ScriptExhausted(Exception)`; `scripted.ScriptedModel(script: dict[str, list[ModelResponse]])` keyed by stage value (`"plan"`, `"execute"`, `"compress"`), consuming each list in order; raises `ScriptExhausted` when a key is missing or empty.
  - `recording.RecordingModelClient(inner: ModelClient, model_calls: ModelCallRepository, request_message_id: str, memory_version: str)` whose `invoke` writes one `ModelCall` per call with `stage`, `model`, `task_id`, `context_version = f"mem:{memory_version}|prompt:{prompt_version(stage)}"`, `usage`, `outcome COMPLETED`, `started_at`, `ended_at`; writes `outcome ERROR` with `failure_reason = str(exc)` and re-raises when `inner.invoke` raises; writes nothing when the response has `replayed=True`.
  - `tools/base.py`: `ToolContext(chat_id: str, user_id: str, request_message_id: str, task_id: str, tool_call_id: str)`; `ToolResult(status: Literal["ok","error","not_executed"] = "ok", rows: list[dict[str, Any]] | None = None, text: str | None = None, error: str | None = None, error_class: Literal["code","system"] | None = None, retryable: bool = False)`; `Tool` Protocol with attributes `spec: ToolSpec`, `permission: Literal["READ","WRITE","DESTRUCTIVE"]` and method `run(arguments: dict[str, Any], ctx: ToolContext) -> ToolResult`. Phase 1 has one implementation (Task 9).
  - `prompts.load_prompt(name: str) -> str` reading `prompts/<name>.md`; `prompts.prompt_version(stage: Stage) -> str` mapping `plan -> "plan.1"`, `execute -> "execute.1"`, `compress -> "compress.1"`, others `-> "none"`; `prompts.MEMORY_VERSION = "1"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_models.py
import pytest
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.scripted import ScriptedModel, ScriptExhausted
from contextforge.core.models.recording import RecordingModelClient
from contextforge.core.repositories.memory import Repositories

def _req(stage: str = "plan") -> ModelRequest:
    return ModelRequest(stage=stage, model="scripted", system=["s"], messages=[])

def test_scripted_model_returns_in_order_then_exhausts() -> None:
    m = ScriptedModel({"plan": [ModelResponse(text="one"), ModelResponse(text="two")]})
    assert m.invoke(_req()).text == "one"
    assert m.invoke(_req()).text == "two"
    with pytest.raises(ScriptExhausted):
        m.invoke(_req())

def test_recording_client_writes_one_model_call_per_invocation() -> None:
    repositories = Repositories.in_memory()
    inner = ScriptedModel({"plan": [ModelResponse(text="x")], "execute": [ModelResponse(text="y", replayed=True)]})
    client = RecordingModelClient(inner, repositories.model_calls, request_message_id="m1", memory_version="1")
    client.invoke(_req("plan"))
    client.invoke(_req("execute"))
    calls = repositories.model_calls.list_by_request("m1")
    assert [c.stage for c in calls] == ["plan"]
    assert calls[0].context_version == "mem:1|prompt:plan.1"

def test_recording_client_records_error_and_reraises() -> None:
    repositories = Repositories.in_memory()
    client = RecordingModelClient(ScriptedModel({}), repositories.model_calls, request_message_id="m1", memory_version="1")
    with pytest.raises(ScriptExhausted):
        client.invoke(_req("plan"))
    assert repositories.model_calls.list_by_request("m1")[0].outcome == "ERROR"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.models`.

- [ ] **Step 3: Implement the four modules**

`ModelRequest`/`ModelResponse` are Pydantic models with `arbitrary_types_allowed=True` for `AnyMessage`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_models.py -v && uv run mypy src`
Expected: 3 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/models src/contextforge/core/context src/contextforge/core/tools tests/test_models.py
git commit -m "feat: model boundary with scripted fake and ModelCall recording"
```

---

### Task 7: Context Builder and Tool Result Processor

**Files:**
- Create: `src/contextforge/core/context/tokens.py`, `budgets.py`, `builder.py`, `tool_results.py`
- Test: `tests/test_context.py`

**Interfaces:**
- Produces:
  - `tokens.estimate_tokens(text: str) -> int` = `max(1, ceil(len(text) / 4))` (phase 1 heuristic; a tokenizer replaces it in phase 2 behind the same signature).
  - `budgets.Budgets(cap_input_tokens, response_allowance_tokens, margin_fraction, keep_last_messages, tool_result_row_cap, facts_cap, call_budget, tasks_in_flight, tool_calls_per_task, task_retries, no_progress_turns, column_cap, cell_char_cap)`; `budgets.load_budgets(path: Path = Path("config/budgets.yaml")) -> Budgets`; method `Budgets.messages_budget(fixed_tokens: int) -> int` = `cap - allowance - ceil(cap * margin) - fixed`.
  - `builder.SourceLine(artifact_id, kind, name, detail: str)`; `builder.ContextInputs(memory: str, stage_prompt: str, request: RequestMessage | None, preferences: dict[str, str], constraints: list[str], facts: list[Fact], open_questions: list[OpenQuestion], next_action: str, dependency_summaries: list[str], evidence_slices: list[str], tools: list[ToolSpec], sources: list[SourceLine], prior_messages: list[Message], loop_messages: list[AnyMessage])`.
  - `builder.BuiltContext(system: list[str], user_text: str, messages: list[AnyMessage], sections: list[tuple[str, int]], fixed_tokens: int)` where `sections` lists the eight section names in spec 8.2 order with their token estimates and `system == [memory, stage_prompt]`.
  - `builder.build_context(inputs: ContextInputs, budgets: Budgets) -> BuiltContext`. `user_text` renders the spec 9.1 tags in order: `<request message_id=...>`, `<user_preferences>`, `<constraints>`, `<sources chat_id=...>`, `<prior_context>`; when `facts` exceed `facts_cap`, only the newest `facts_cap` are rendered.
  - `tool_results.ProcessedRows(rows: list[dict[str, Any]], total_rows: int, artifact_id: str | None, text: str)`; `tool_results.process_rows(rows, *, cap: int, artifacts: ArtifactRepository, blobs: BlobStore, chat_id, user_id, name: str, artifact_id: str) -> ProcessedRows`: the caller passes `artifact_id` derived as `raw_result_id(task_id, tool_call_id)` (a ULID-shaped deterministic hash, `tool_results.raw_result_id`) so a replayed tool call overwrites rather than duplicates; at most `cap` rows kept; when `len(rows) > cap` the full rows are written as JSON lines to the blob store, a `raw_result` Artifact at `AVAILABLE` is saved (upsert), and `text` is `f"showing {cap} of {len(rows)} rows; {len(rows) - cap} more row(s) in artifact {artifact_id}"`; when `len(rows) <= cap`, `artifact_id is None` and `text` is `f"showing {len(rows)} of {len(rows)} rows"` (so zero rows reads "showing 0 of 0 rows"). Kept rows are shaped once: at most `column_cap` columns and `cell_char_cap` characters per cell with the marker `…[+N chars]`; the raw_result Artifact holds the unshaped rows.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_context.py
from contextforge.core.context.budgets import Budgets, load_budgets
from contextforge.core.context.builder import ContextInputs, SourceLine, build_context
from contextforge.core.context.tool_results import process_rows
from contextforge.core.repositories.memory import Repositories
from contextforge.core.storage import MemoryBlobStore

def test_budgets_messages_budget() -> None:
    b = load_budgets()
    assert b.messages_budget(fixed_tokens=2500) == 40000 - 4000 - 4000 - 2500

def test_builder_sections_in_spec_order_and_system_blocks() -> None:
    inputs = ContextInputs(memory="MEM", stage_prompt="PLAN", request=None, preferences={"currency": "INR"},
        constraints=["c1"], facts=[], open_questions=[], next_action="n", dependency_summaries=[],
        evidence_slices=[], tools=[], sources=[SourceLine("a2", "dataset", "t.parquet", "rows: 60")],
        prior_messages=[], loop_messages=[])
    built = build_context(inputs, load_budgets())
    assert built.system == ["MEM", "PLAN"]
    assert [name for name, _ in built.sections] == ["memory", "brief", "facts", "open_questions",
        "dependencies", "evidence", "tools", "messages"]
    assert "<sources chat_id=" in built.user_text and "a2  dataset" in built.user_text

def test_facts_are_capped_to_newest() -> None:
    facts = [{"text": f"f{i}", "evidence_refs": []} for i in range(35)]
    inputs = ContextInputs(memory="M", stage_prompt="P", request=None, preferences={}, constraints=[],
        facts=facts, open_questions=[], next_action="n", dependency_summaries=[], evidence_slices=[],
        tools=[], sources=[], prior_messages=[], loop_messages=[])
    text = build_context(inputs, load_budgets()).user_text
    assert "f34" in text and "f4" not in text

def test_process_rows_boundary_at_cap() -> None:
    repositories, blobs = Repositories.in_memory(), MemoryBlobStore()
    fifty = [{"i": i} for i in range(50)]
    r = process_rows(fifty, cap=50, artifacts=repositories.artifacts, blobs=blobs, chat_id="c1", user_id="u1", name="q", artifact_id="a6")
    assert r.artifact_id is None and len(r.rows) == 50 and r.text == "showing 50 of 50 rows"
    r2 = process_rows(fifty + [{"i": 50}], cap=50, artifacts=repositories.artifacts, blobs=blobs,
                      chat_id="c1", user_id="u1", name="q", artifact_id="a7")
    r3 = process_rows(fifty + [{"i": 50}], cap=50, artifacts=repositories.artifacts, blobs=blobs,
                      chat_id="c1", user_id="u1", name="q", artifact_id="a7")   # replay: same id, one artifact
    assert r3.artifact_id == "a7" and len(repositories.artifacts.list_by_chat("c1")) == 1
    assert r2.artifact_id and len(r2.rows) == 50 and r2.text == f"showing 50 of 51 rows; 1 more row(s) in artifact {r2.artifact_id}"
    art = repositories.artifacts.get("c1", r2.artifact_id)
    assert art and art.kind == "raw_result" and art.status == "AVAILABLE"
    assert blobs.get(art.storage_ref).count(b"\n") == 51

def test_process_rows_1240_rows_keeps_50() -> None:
    repositories, blobs = Repositories.in_memory(), MemoryBlobStore()
    r = process_rows([{"i": i} for i in range(1240)], cap=50, artifacts=repositories.artifacts, blobs=blobs,
                     chat_id="c1", user_id="u1", name="q", artifact_id="a8")
    assert len(r.rows) == 50 and r.total_rows == 1240 and "1190 more row(s)" in r.text

def test_zero_rows_is_a_valid_result() -> None:
    repositories, blobs = Repositories.in_memory(), MemoryBlobStore()
    r = process_rows([], cap=50, artifacts=repositories.artifacts, blobs=blobs, chat_id="c1", user_id="u1", name="q", artifact_id="a8")
    assert r.rows == [] and r.artifact_id is None and r.text == "showing 0 of 0 rows"

def test_wide_cells_are_shaped_once() -> None:
    repositories, blobs = Repositories.in_memory(), MemoryBlobStore()
    r = process_rows([{"c": "x" * 500}], cap=50, artifacts=repositories.artifacts, blobs=blobs, chat_id="c1", user_id="u1", name="q", artifact_id="a9")
    assert r.rows[0]["c"].endswith("…[+300 chars]") and len(r.rows[0]["c"]) == 200 + len("…[+300 chars]")

def test_build_context_is_byte_stable() -> None:
    inputs = _inputs()
    assert build_context(inputs, load_budgets()).user_text == build_context(inputs, load_budgets()).user_text
    assert build_context(inputs, load_budgets()).system == build_context(inputs, load_budgets()).system
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_context.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.context.budgets`.

- [ ] **Step 3: Implement the four modules**

`build_context` computes `fixed_tokens` as the sum of sections 1 to 7 and leaves `messages` untouched (compaction is Task 11's job and runs before the builder is called).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_context.py -v && uv run mypy src`
Expected: 5 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/context tests/test_context.py
git commit -m "feat: context builder with eight sections and tool result processor"
```

---

### Task 8: Plan stage, plan gate, cancel and budget guards

**Files:**
- Create: `src/contextforge/worker/dependencies.py`, `src/contextforge/worker/stages/__init__.py`, `src/contextforge/worker/stages/plan.py`, `src/contextforge/worker/stages/guards.py`, `src/contextforge/core/context/resolver.py`
- Test: `tests/test_plan_stage.py`

**Interfaces:**
- Consumes: `ModelClient`, `Repositories`, `Budgets`, `build_context`, `PlannerOutput`, `RequestState`, `PendingInterrupt`.
- Produces:
  - `deps.WorkerDependencies(repos: Repositories, blobs: BlobStore, model: ModelClient, budgets: Budgets, tools: dict[str, Tool], queue: Queue, planner_model: str = "scripted", executor_model: str = "scripted")` dataclass.
  - `guards.Cancelled(Exception)`; `guards.check_not_cancelled(deps: WorkerDependencies, chat_id: str, request_message_id: str) -> None` raising `Cancelled` when the request Message status is `CANCELLED`. Called before every model call and before every wave of tasks.
  - `guards.CallBudgetExceeded(Exception)`; `guards.check_call_budget(deps: WorkerDependencies, request_message_id: str) -> None` raising when `len(deps.repos.model_calls.list_by_request(request_message_id)) >= deps.budgets.call_budget`. Called before every model call.
  - `plan.PlanFailed(Exception)` with `.reason`.
  - `resolver.ResolvedContext(period_start: str | None, period_end: str | None, today: str, sources: list[SourceStatus])` and `resolver.resolve_context(request_text: str, artifacts: list[Artifact], *, today: date, timezone: str) -> ResolvedContext`: a pure function that recognises month names and "last month" style phrases in the request (a handful of patterns, no library) and returns an explicit window in the user's timezone, plus each artifact's id and index status. `create_research_plan` renders it as the `<context>` block at the top of the user turn and copies the window into `constraints` when found. Dates never enter the system prompt.
  - `plan.create_research_plan(brief: RequestState, attempt: int, deps: WorkerDependencies) -> dict`: a plain function (the workflow wraps it as a task in Task 9). Builds context from the request Message, the chat's AVAILABLE artifacts as sources, the user's preferences and prior Messages in the chat; when `attempt >= 2` appends the constraint `"Do not ask another question; plan with stated assumptions"`; calls `check_not_cancelled` and `check_call_budget`; invokes the model with `stage=plan`, `output_schema=PlannerOutput`; parses `response.structured` into `PlannerOutput`. Returns a JSON-serialisable dict `{"kind", "goal", "constraints", "open_questions", "next_action", "tasks", "pending"}`. On `kind == "plan"`, `pending` is `None` and `tasks` is the dumped task list. On `kind == "clarification"`: when `attempt >= 2` raises `PlanFailed("planner asked a third question")`; otherwise saves a `ClarificationMessage` (sender assistant, `request_message_id`, content = question) whose id is `clarification_id(request_message_id, attempt)` (a deterministic ULID-shaped hash in `plan.py`, so a re-run cannot write a second one), moves the request `RUNNING -> WAITING_USER`, and returns `pending = PendingInterrupt(kind="clarification", message_id=<id>, question=<question>, next_action=<planner next_action>)`. It never calls `interrupt()`. On a schema failure it invokes the model once more with the validation errors appended as a constraint (`"Previous plan was rejected: ..."`); a second failure, `ScriptExhausted` or `CallBudgetExceeded` raises `PlanFailed(reason)`.
  - `plan.validate_and_save_tasks(brief: RequestState, planned: dict, deps: WorkerDependencies) -> list[PlanEntry]`: for each planned task verifies every `input_artifact_id` is an Artifact in `brief["chat_id"]` with status `AVAILABLE` and that `depends_on` refs resolve; on failure raises `PlanFailed("Planned task t2 names artifact a9 that is not in this chat")` having created nothing; on success creates one `Task` per planned task at `PLANNED` with new ULIDs assigned in plan order and `depends_on_task_ids` mapped from refs, and returns the `PlanEntry` list in the same order. Idempotent: when Tasks already exist for the request (a re-run after a crash between the gate and its checkpoint), returns entries for the existing Tasks and creates none.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_plan_stage.py
import pytest
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.worker.stages.plan import create_research_plan, validate_and_save_tasks, PlanFailed
from tests.helpers import make_deps, seed_chat_with_dataset, create_initial_request_state  # written in this task

def _plan_research_tasks(ids: list[str]) -> dict:
    return {"kind": "plan", "goal": "g", "constraints": ["Exclude transfers"], "openQuestions": ["q"],
            "tasks": [{"ref": "t1", "kind": "sql_analysis", "goal": "g1", "inputArtifactIds": ids, "dependsOn": []},
                      {"ref": "t2", "kind": "reconcile", "goal": "g2", "inputArtifactIds": [], "dependsOn": ["t1"]}],
            "nextAction": "run t1"}

def _clarification(q: str) -> dict:
    return {"kind": "clarification", "goal": "g", "constraints": [], "openQuestions": [], "question": q, "nextAction": "wait"}

def test_plan_then_gate_creates_tasks_in_order() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_plan_research_tasks(["a2"]))]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    request_state = create_initial_request_state(req)
    planned = create_research_plan(request_state, 0, worker_dependencies)
    assert planned["kind"] == "plan" and planned["constraints"] == ["Exclude transfers"] and planned["next_action"] == "run t1"
    entries = validate_and_save_tasks(request_state, planned, worker_dependencies)
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert [t.kind for t in tasks] == ["sql_analysis", "reconcile"]
    assert tasks[1].depends_on_task_ids == [tasks[0].task_id]
    assert [e["task_id"] for e in entries] == [t.task_id for t in tasks]
    assert validate_and_save_tasks(request_state, planned, worker_dependencies) == entries          # re-run creates nothing new
    assert len(worker_dependencies.repositories.tasks.list_by_request(req.message_id)) == 2

def test_gate_rejects_artifact_from_another_chat() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_plan_research_tasks(["a9"]))]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    seed_chat_with_dataset(worker_dependencies, chat_id="c2", user_id="u1", dataset_id="a9")
    planned = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    with pytest.raises(PlanFailed) as e:
        validate_and_save_tasks(create_initial_request_state(req), planned, worker_dependencies)
    assert "a9" in e.value.reason and worker_dependencies.repositories.tasks.list_by_request(req.message_id) == []

def test_clarification_writes_one_message_and_pending_payload() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_clarification("Which accounts are yours?"))] * 2}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    worker_dependencies.repositories.messages.update_request_status("c1", req.message_id, "ACCEPTED", "RUNNING")
    first = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert first["kind"] == "clarification" and first["pending"]["kind"] == "clarification"
    assert first["pending"]["question"] == "Which accounts are yours?" and first["pending"]["next_action"] == "wait"
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "WAITING_USER"
    worker_dependencies.repositories.messages.update_request_status("c1", req.message_id, "WAITING_USER", "RUNNING")
    second = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)            # same attempt re-run = same message id
    assert second["pending"]["message_id"] == first["pending"]["message_id"]
    assert len([m for m in worker_dependencies.repositories.messages.list_by_chat("c1") if m.kind == "clarification"]) == 1

def test_third_question_fails_the_plan() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_clarification("Again?"))]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed):
        create_research_plan(create_initial_request_state(req), 2, worker_dependencies)

def test_malformed_planner_output_fails_plainly() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured={"kind": "plan"})]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed):
        create_research_plan(create_initial_request_state(req), 0, worker_dependencies)

def test_planner_is_retried_once_with_its_errors() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured={"kind": "plan"}), ModelResponse(structured=_plan_research_tasks(["a2"]))]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    planned = create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert planned["kind"] == "plan" and len(worker_dependencies.repositories.model_calls.list_by_request(req.message_id)) == 2

def test_resolver_turns_months_into_a_window() -> None:
    from datetime import date
    from contextforge.core.context.resolver import resolve_context
    r = resolve_context("Compare October spending with September", [], today=date(2026, 11, 7), timezone="Asia/Kolkata")
    assert (r.period_start, r.period_end) == ("2026-09-01", "2026-10-31") and r.today == "2026-11-07"

def test_call_budget_stops_the_planner() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=_plan_research_tasks(["a2"]))]}), call_budget=0)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    with pytest.raises(PlanFailed) as e:
        create_research_plan(create_initial_request_state(req), 0, worker_dependencies)
    assert "budget" in e.value.reason
```

`tests/helpers.py` provides `make_deps(model, *, call_budget: int | None = None, tasks_in_flight: int | None = None) -> WorkerDependencies` (in-memory repos, `MemoryBlobStore`, `load_budgets()` with the two overrides applied through `dataclasses.replace`), `seed_chat_with_dataset(deps, *, chat_id, user_id, dataset_id) -> RequestMessage` (saves a Chat, a `dataset` Artifact at AVAILABLE named `transactions_sep_oct.csv` with `storage_ref` pointing at the fixture CSV copied into the blob store, and a request Message at ACCEPTED with the fixture request text) and `create_initial_request_state(req) -> RequestState` (identity fields filled, every brief field empty).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plan_stage.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.worker.stages`.

- [ ] **Step 3: Implement `deps.py`, `guards.py`, `plan.py` and `tests/helpers.py`**

`clarification_id` hashes `f"{request_message_id}:clarification:{attempt}"` the same way `raw_result_id` hashes its key (Task 7); put the shared helper in `core/ids.py` as `derived_id(key: str) -> str` and make `raw_result_id` call it.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_plan_stage.py -v && uv run mypy src`
Expected: 8 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/ids.py src/contextforge/worker tests/helpers.py tests/test_plan_stage.py
git commit -m "feat: plan stage with clarification payload, plan gate, cancel and budget guards"
```

---

### Task 9: SQL tool, task subgraph, execute stage, stubs, report, workflow entrypoint

**Files:**
- Create: `src/contextforge/worker/tools/__init__.py`, `sql_query.py`; `src/contextforge/worker/stages/execute.py`, `stubs.py`, `citation_checks.py`, `report.py`; `src/contextforge/worker/workflow.py`
- Test: `tests/test_workflow.py`

**Interfaces:**
- Consumes: Task 7 `process_rows`, `raw_result_id`, `build_context`; Task 8 stages and guards; `TaskState`; `entrypoint`, `task` from `langgraph.func`; `interrupt` from `langgraph.types`.
- Produces:
  - `sql_query.SqlQueryTool(artifacts: ArtifactRepository, blobs: BlobStore)` implementing `Tool`: `spec` is the exact name, description and `input_schema` from spec 9.2; `permission = "READ"`; `run(arguments, ctx)` loads the Artifact `arguments["dataset_id"]` in `ctx.chat_id`, reads its CSV bytes from the blob store into a DuckDB in-memory connection as table `t` (`read_csv_auto`), rejects any statement that is not a single `SELECT` (DuckDB `EXPLAIN` parse plus a first-token check; a convenience guard, not a security boundary), returns `ToolResult(rows=...)`.
  - `execute.build_task_graph(deps: WorkerDependencies) -> CompiledStateGraph` over `TaskState` with two nodes. `call_model`: calls `check_not_cancelled` and `check_call_budget`; builds context with the execute prompt, the task goal, constraints, the dataset source line with columns, dependency summaries and the specs of the tools allowed for the task kind; invokes `stage=execute`; appends an `AIMessage` with `tool_calls` or, on `end_turn`, parses `response.structured` into `TaskResult`, calls `validate_structured_data`, drops any fact whose evidence ref names an artifact outside the task's `input_artifact_ids` and keeps at most six, sets `result`. `run_tools`: for each tool call looks the tool up in `deps.tools` by name, runs it with a `ToolContext`, passes rows through `process_rows` with `cap=budgets.tool_result_row_cap` and `artifact_id=raw_result_id(task_id, tool_call_id)`, appends a `ToolMessage` whose content is `ProcessedRows.text` plus the kept rows as JSON and whose `additional_kwargs` carry `tool_name`, `arguments`, `row_count`, `artifact_id`. Edges: `call_model -> run_tools` when the last message has tool calls and fewer than `budgets.tool_calls_per_task` tool calls have been made, `call_model -> END` otherwise. Loop control: before the last allowed call `run_tools` appends a `HumanMessage("[runtime] one tool call remains; return your structured result after it")`; a tool call past the cap returns a `not_executed` ToolMessage and the next `call_model` carries the same nudge with "no tool calls remain"; if the model still returns no result the task ends with `error = "tool call budget exhausted"`. A tool call whose `(name, canonical json of arguments)` was already made in this task returns the earlier ToolMessage content prefixed "[already done]" and does not count against the cap. `no_progress_turns` consecutive `call_model` turns that add no new fact and no new artifact end the task with `error = "no progress"`. A tool that raises or returns `status: error` becomes a ToolMessage with the envelope (`status`, `error`, `error_class`, `retryable`) and the loop continues; a model error, `CallBudgetExceeded` or schema failure sets `error` and ends; `Cancelled` propagates.
  - `execute.execute_task(brief: RequestState, task_id: str, deps: WorkerDependencies) -> dict`: a plain function wrapped as a task by the workflow; runs under `deps.semaphore` (a `threading.BoundedSemaphore(budgets.tasks_in_flight)` created in `WorkerDependencies.__post_init__`). Loads the Task; transitions `PLANNED -> RUNNING`; on `StaleStatusError` returns the Task's stored outcome unchanged when its status is already `COMPLETED`, `SKIPPED` or `FAILED` (a re-run after a crash between the repository write and the checkpoint) and raises otherwise. For `sql_analysis`: invokes the subgraph with a fresh `TaskState` whose `dependency_summaries` are the dependencies' result summaries read from the repository; on `error` invokes it once more with a fresh `TaskState` (the one automatic retry); on `result` transitions `RUNNING -> COMPLETED` with the result, on a second `error` transitions `RUNNING -> FAILED` with `failure_reason = error`. For `reconcile`: `COMPLETED` with `reconcile_stub` when every dependency is `COMPLETED`, else `FAILED` with `failure_reason = "dependency failed"`. For `pdf_lookup` and `source_search`: `SKIPPED` with `failure_reason = skipped_reason(kind)`. Returns `{"task_id", "status", "facts": <result facts or []>, "uncertainties": [...], "summary": str | None}`.
  - `stubs.reconcile_stub(task: Task, dependency_results: list[TaskResult]) -> TaskResult`: summary joins dependency summaries with `"; "`, `structured_data={"reconciled": False}`, `uncertainties=["reconcile is a stub in phase 1"]`, `evidence_refs` = union of dependencies' refs, `facts=[]`.
  - `stubs.skipped_reason(kind: TaskKind) -> str` = `f"{kind} is not implemented in phase 1"`.
  - `cite.cite_stage(brief: RequestState, deps: WorkerDependencies) -> list[Fact]`: phase 1 stub returning `brief["facts"]` unchanged. Phase 3 replaces the body with the evidence re-read and claim call.
  - `report.write_report(brief: RequestState, outcomes: list[dict], deps: WorkerDependencies) -> str`: builds a `ReportMessage` with id `derived_id(f"{request_message_id}:report")` whose `claims` are one per fact (`claim_id` = `derived_id(f"{report_id}:{i}")`, `text`, `evidence_refs`), `inputs_consulted` = every Artifact referenced by any fact, `uncertainties` = union of outcome uncertainties plus one line per SKIPPED or FAILED task, `coverage={"tasks": {"completed": n, "skipped": n, "failed": n}}`; saves it (an upsert, so a re-run rewrites the same Message); moves the request `RUNNING -> COMPLETED` when at least one task COMPLETED, else `RUNNING -> FAILED` with `failure_reason = "every task failed"`; returns the final status.
  - `workflow.build_research_workflow(deps: WorkerDependencies, checkpointer: BaseCheckpointSaver | None) -> Pregel`: wraps each stage once, `plan_t = task(name="plan")(partial(create_research_plan, deps=deps))` and likewise `gate_t`, `execute_t`, `cite_t`, `report_t`, then returns `entrypoint(checkpointer=checkpointer)(run_request)` where `run_request(inputs: dict) -> dict` is:
    1. `brief = load_initial_request_state(inputs["chat_id"], inputs["message_id"])` (identity fields, empty brief).
    2. `attempt = 0`; loop: `planned = plan_t(brief, attempt).result()`; if `planned["kind"] == "plan"` break; `answer = interrupt(planned["pending"])`; `brief["constraints"].append(f"User answered '{planned['pending']['question']}': {answer['content']}")`; `attempt += 1`. The resume payload is `{"message_id", "content"}`. Every statement in this loop is deterministic given restored task results, so the interrupt index is stable across resumes.
    3. Copy `goal`, `constraints`, `open_questions`, `next_action` from `planned` into `request_state`; `brief["plan"] = gate_t(brief, planned).result()`.
    4. Waves: `done: dict[str, dict] = {}`; while tasks remain, `check_not_cancelled`; `ready` = plan entries not in `done` whose dependencies are all in `done`, in `task_id` order; `futures = [execute_t(brief, e["task_id"]) for e in ready]`; `for f in futures: outcome = f.result(); done[outcome["task_id"]] = outcome; brief["facts"] += outcome["facts"]` (trimmed to the newest `budgets.facts_cap`); refresh `brief["plan"]` statuses. Futures are created in `task_id` order and waited in the same order, so replay restores them by name and call order.
    5. `brief["facts"] = cite_t(brief).result()`; `status = report_t(brief, list(done.values())).result()`; `return {"status": status}`.
    `PlanFailed` is caught: the request moves `RUNNING -> FAILED` with `failure_reason = reason` and the entrypoint returns `{"status": "FAILED"}`. `Cancelled` is caught: returns `{"status": "CANCELLED"}` without changing the status (the API already set it). Any other exception propagates so the checkpoint keeps the completed task results for a later `start`.
  - `workflow.load_initial_request_state(deps, chat_id, message_id) -> RequestState`, shared with `tests/helpers.create_initial_request_state`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_workflow.py
import threading
from contextforge.core.models.base import ModelResponse, ToolCall
from contextforge.core.models.scripted import ScriptedModel
from contextforge.worker.workflow import build_research_workflow
from contextforge.worker.tools.sql_query import SqlQueryTool
from contextforge.core.tools.base import ToolContext
from tests.helpers import make_deps, seed_chat_with_dataset, six_task_plan, t1_result

def _cfg(req) -> dict:
    return {"configurable": {"thread_id": req.message_id}}

def _start(worker_dependencies, req) -> None:
    worker_dependencies.repositories.messages.update_request_status("c1", req.message_id, "ACCEPTED", "RUNNING")

def test_csv_sql_query_finds_planted_transfer_pairs() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    tool_context = ToolContext(chat_id="c1", user_id="u1", request_message_id=req.message_id, task_id="t1", tool_call_id="x1")
    rows = SqlQueryTool(worker_dependencies.repositories.artifacts, worker_dependencies.blobs).run({"dataset_id": "a2", "sql":
        "SELECT d.row_id AS debit_row, c.row_id AS credit_row, d.amount FROM t d JOIN t c "
        "ON d.amount = c.amount AND d.direction='DEBIT' AND c.direction='CREDIT' "
        "AND d.account_id <> c.account_id AND abs(date_diff('day', d.txn_date, c.txn_date)) <= 2"}, tool_context).rows
    assert rows is not None
    assert {(r["debit_row"], r["credit_row"]) for r in rows} >= {(12, 13), (40, 41), (50, 51)}

def test_fixture_request_produces_six_tasks_and_a_report() -> None:
    script = {"plan": [ModelResponse(structured=six_task_plan())],
              "execute": [ModelResponse(tool_calls=[ToolCall(id="x1", name="sql_query",
                              arguments={"dataset_id": "a2", "sql": "SELECT * FROM t LIMIT 5"})], stop_reason="tool_use"),
                          ModelResponse(structured=t1_result()),
                          ModelResponse(structured=t1_result()),
                          ModelResponse(structured=t1_result())]}
    worker_dependencies = make_deps(ScriptedModel(script), tasks_in_flight=1)   # serial, so the script order is deterministic
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke({"chat_id": "c1", "message_id": req.message_id}, _cfg(req))
    assert out["status"] == "COMPLETED"
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    assert len(tasks) == 6
    assert sorted(t.status for t in tasks) == sorted(["COMPLETED"] * 4 + ["SKIPPED"] * 2)
    msgs = worker_dependencies.repositories.messages.list_by_chat("c1")
    report = [m for m in msgs if m.kind == "report"][0]
    assert report.report.claims and all(c.evidence_refs for c in report.report.claims)
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "COMPLETED"
    assert len(worker_dependencies.repositories.model_calls.list_by_request(req.message_id)) == 5

def test_independent_tasks_run_in_parallel() -> None:
    gate = threading.Barrier(3, timeout=5)        # all three sql tasks must be inside the model at once
    class Pausing(ScriptedModel):
        def invoke(self, request):
            if request.stage == "execute":
                gate.wait()
            return super().invoke(request)
    plan = six_task_plan(); plan["tasks"][1]["dependsOn"] = []     # t1, t2, t3 all independent
    worker_dependencies = make_deps(Pausing({"plan": [ModelResponse(structured=plan)],
                              "execute": [ModelResponse(structured=t1_result())] * 3}), tasks_in_flight=3)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke({"chat_id": "c1", "message_id": req.message_id}, _cfg(req))
    assert out["status"] == "COMPLETED" and gate.broken is False

def test_cancel_between_waves_stops_the_run() -> None:
    script = {"plan": [ModelResponse(structured=six_task_plan())],
              "execute": [ModelResponse(structured=t1_result())] * 3}
    worker_dependencies = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    # the API sets CANCELLED; here the hook fires right after the first task completes
    worker_dependencies.repositories.tasks.on_save = lambda t: worker_dependencies.repositories.messages.update_request_status(  # type: ignore[attr-defined]
        "c1", req.message_id, "RUNNING", "CANCELLED") if t.status == "COMPLETED" and t.kind == "sql_analysis" else None
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke({"chat_id": "c1", "message_id": req.message_id}, _cfg(req))
    assert out["status"] == "CANCELLED"
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "CANCELLED"
    assert not [m for m in worker_dependencies.repositories.messages.list_by_chat("c1") if m.kind == "report"]

def test_tool_call_cap_fails_the_task_not_the_request() -> None:
    call = ModelResponse(tool_calls=[ToolCall(id="x", name="sql_query",
                         arguments={"dataset_id": "a2", "sql": "SELECT 1"})], stop_reason="tool_use")
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())],
                                    "execute": [call] * 14 + [ModelResponse(structured=t1_result())] * 2}), tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke({"chat_id": "c1", "message_id": req.message_id}, _cfg(req))
    tasks = worker_dependencies.repositories.tasks.list_by_request(req.message_id)
    failed = [t for t in tasks if t.status == "FAILED"]
    assert out["status"] == "COMPLETED" and len(failed) >= 1 and "tool call budget" in failed[0].failure_reason

def test_tool_error_is_a_result_the_model_sees() -> None:
    class Boom:
        spec = SqlQueryTool.spec; permission = "READ"
        def run(self, arguments, tool_context): raise RuntimeError("duckdb exploded")
    boom = ModelResponse(tool_calls=[ToolCall(id="x", name="sql_query", arguments={"dataset_id": "a2", "sql": "SELECT 1"})], stop_reason="tool_use")
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())],
                                    "execute": [boom, ModelResponse(structured=t1_result())] * 3}), tasks_in_flight=1)
    worker_dependencies.tools["sql_query"] = Boom()
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    seen = []
    worker_dependencies.model.on_request = lambda r: seen.extend(m.content for m in r.messages if m.type == "tool")  # type: ignore[attr-defined]
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke({"chat_id": "c1", "message_id": req.message_id}, _cfg(req))
    assert out["status"] == "COMPLETED" and any('"error_class": "system"' in c for c in seen)

def test_duplicate_tool_call_spends_no_slot() -> None:
    call = ModelResponse(tool_calls=[ToolCall(id="x", name="sql_query", arguments={"dataset_id": "a2", "sql": "SELECT 1"})], stop_reason="tool_use")
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())],
                                    "execute": [call] * 8 + [ModelResponse(structured=t1_result())] * 3}), tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    build_research_workflow(worker_dependencies, checkpointer=None).invoke({"chat_id": "c1", "message_id": req.message_id}, _cfg(req))
    t1 = worker_dependencies.repositories.tasks.list_by_request(req.message_id)[0]
    assert t1.status == "FAILED" and t1.failure_reason == "no progress"     # the duplicate never counted; no-progress ended it

def test_scripted_exhaustion_fails_request_with_reason() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    _start(worker_dependencies, req)
    out = build_research_workflow(worker_dependencies, checkpointer=None).invoke({"chat_id": "c1", "message_id": req.message_id}, _cfg(req))
    m = worker_dependencies.repositories.messages.get("c1", req.message_id)
    assert out["status"] == "FAILED" and m.status == "FAILED" and m.failure_reason
    assert any(c.outcome == "ERROR" for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id))
```

Add to `tests/helpers.py`: `six_task_plan() -> dict` (t1, t2, t3 `sql_analysis` on `a2` with t2 depending on t1; t4 `pdf_lookup`; t5 `source_search`; t6 `reconcile` depending on t1, t2, t3) and `t1_result() -> dict` (the spec 9.2 result trimmed to two transfer pairs, the card settlement pair, two evidence refs on `a2`, two facts with those refs, one uncertainty).

The tool-cap test: with the cap at six, t1 consumes six calls then fails on the seventh, retries once (six more, fails again), so fourteen tool-call responses are consumed before t2 and t3 complete; t6 then fails on its dependency, and the request still completes with the two failures under uncertainties.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_workflow.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.worker.workflow`.

- [ ] **Step 3: Implement the tool, stages and workflow**

The subgraph is compiled without a checkpointer (its transcript is dropped on completion by design, spec 8.6 level 2). `InMemoryTasks` gains an optional `on_save` hook, called after the task is stored, used only by tests; `ScriptedModel` gains an optional `on_request` hook called with each `ModelRequest`, also tests only. `make_deps` registers `{"sql_query": SqlQueryTool(...)}` in `deps.tools` and an `InProcessQueue`. `config/budgets.yaml` gains `callBudget: 50`, `tasksInFlight: 5`, `toolCallsPerTask: 6` and `taskRetries: 1`; `Budgets` gains the matching fields and the Task 1 test of the YAML keys is extended.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_workflow.py -v && uv run mypy src && uv run ruff check .`
Expected: 8 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add config/budgets.yaml src/contextforge tests
git commit -m "feat: execute stage with task subgraph, stubs, report, Functional API workflow"
```

---

### Task 10: Runner, SQLite checkpointing, interrupt and resume, duplicate protection

**Files:**
- Create: `src/contextforge/worker/runner.py`
- Test: `tests/test_runner.py`

**Interfaces:**
- Consumes: `build_research_workflow`, `SqliteSaver` from `langgraph.checkpoint.sqlite`, `Command` from `langgraph.types`.
- Produces:
  - `runner.RunOutcome(started: bool, status: RequestStatus | None = None, reason: str | None = None)`.
  - `runner.RequestRunner(deps: WorkerDependencies, checkpointer: BaseCheckpointSaver)` holding one workflow from `build_research_workflow(deps, checkpointer)`, with:
    - `start(chat_id, message_id) -> RunOutcome`: transitions the request `ACCEPTED -> RUNNING` with the conditional update; on `StaleStatusError` returns `RunOutcome(started=False, reason="already started")` without invoking the workflow. Otherwise, with `config = {"configurable": {"thread_id": message_id}}`: when `workflow.get_state(config).values` is empty (no checkpoint) invokes `{"chat_id", "message_id"}`; when a checkpoint exists (an operator requeue after a crash) invokes `None`, which continues the thread with completed task results restored. Returns `RunOutcome(started=True, status=<returned status>)`.
    - `pending_interrupt(message_id) -> PendingInterrupt | None`: the `value` of the first interrupt in `workflow.get_state(config).interrupts` (fall back to `tasks[*].interrupts` if the attribute is absent in the installed LangGraph), else `None`.
    - `resume(chat_id, message_id, reply: ClarificationResponseMessage | ActionResponseMessage) -> RunOutcome`: verifies the request is `WAITING_USER` and `pending_interrupt(message_id)["message_id"] == reply.in_reply_to_message_id`, else returns `RunOutcome(started=False, reason="reply does not target the pending interrupt")` and changes nothing; otherwise saves the reply Message, transitions `WAITING_USER -> RUNNING`, and invokes `Command(resume={"message_id": reply.message_id, "content": reply.content})` with the same config. The entrypoint re-runs from its first line; the plan task result is restored, the interrupt returns the answer, and the second planner call is a fresh task. The queue is not used in phase 1 (the API service uses it from phase 4).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runner.py
import sqlite3
from langgraph.checkpoint.sqlite import SqliteSaver
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.entities.message import ClarificationResponseMessage
from contextforge.worker.runner import RequestRunner
from tests.helpers import make_deps, seed_chat_with_dataset, six_task_plan, t1_result, clarification

def _saver(path) -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(str(path), check_same_thread=False))

def _reply(req, target: str, content: str = "ACC-SAV-1 and ACC-CUR-2") -> ClarificationResponseMessage:
    return ClarificationResponseMessage(message_id="m3", chat_id="c1", user_id="u1", sender="user",
        kind="clarification_response", content=content, request_message_id=req.message_id,
        in_reply_to_message_id=target, created_at="t", updated_at="t")

def test_duplicate_start_runs_once() -> None:
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())],
                                    "execute": [ModelResponse(structured=t1_result())] * 3}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    runner = RequestRunner(worker_dependencies, _saver(":memory:"))
    first = runner.start("c1", req.message_id)
    second = runner.start("c1", req.message_id)
    assert first.started and first.status == "COMPLETED" and not second.started
    assert len([c for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id) if c.stage == "plan"]) == 1

def test_clarification_interrupt_then_resume_completes(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {"plan": [ModelResponse(structured=clarification("Which accounts are yours?")),
                       ModelResponse(structured=six_task_plan())],
              "execute": [ModelResponse(structured=t1_result())] * 3}
    worker_dependencies = make_deps(ScriptedModel(script))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "WAITING_USER"
    pending = worker_dependencies.repositories.messages.pending_for_request("c1", req.message_id)
    assert len(pending) == 1 and pending[0].kind == "clarification"
    # new runner instance = process restart; same sqlite file = same checkpoint
    out = RequestRunner(worker_dependencies, _saver(db)).resume("c1", req.message_id, _reply(req, pending[0].message_id))
    assert out.started and worker_dependencies.repositories.messages.get("c1", req.message_id).status == "COMPLETED"
    assert len(worker_dependencies.repositories.tasks.list_by_request(req.message_id)) == 6
    plans = [c for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id) if c.stage == "plan"]
    assert len(plans) == 2                          # the first planner call was restored, not re-paid

def test_two_clarifications_then_plan(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {"plan": [ModelResponse(structured=clarification("Which accounts?")),
                       ModelResponse(structured=clarification("Which months?")),
                       ModelResponse(structured=six_task_plan())],
              "execute": [ModelResponse(structured=t1_result())] * 3}
    worker_dependencies = make_deps(ScriptedModel(script))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    for i in range(2):
        pending = [m for m in worker_dependencies.repositories.messages.pending_for_request("c1", req.message_id)]
        assert len(pending) == 1
        out = RequestRunner(worker_dependencies, _saver(db)).resume("c1", req.message_id, _reply(req, pending[0].message_id, f"answer {i}"))
        assert out.started
    assert worker_dependencies.repositories.messages.get("c1", req.message_id).status == "COMPLETED"
    assert len([m for m in worker_dependencies.repositories.messages.list_by_chat("c1") if m.kind == "clarification"]) == 2

def test_resume_with_wrong_target_is_refused(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=clarification("Which?"))]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    out = RequestRunner(worker_dependencies, _saver(db)).resume("c1", req.message_id, _reply(req, "not-the-pending-one"))
    assert not out.started and worker_dependencies.repositories.messages.get("c1", req.message_id).status == "WAITING_USER"
    assert not [m for m in worker_dependencies.repositories.messages.list_by_chat("c1") if m.kind == "clarification_response"]

def test_restart_after_crash_does_not_replay_finished_tasks(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    script = {"plan": [ModelResponse(structured=six_task_plan())],
              "execute": [ModelResponse(structured=t1_result())] * 3}
    worker_dependencies = make_deps(ScriptedModel(script), tasks_in_flight=1)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    completed = []
    def crash_after_second_completed(t):
        if t.kind == "sql_analysis" and t.status == "COMPLETED":
            completed.append(t.task_id)
            if len(completed) == 2:
                raise RuntimeError("crash")
    worker_dependencies.repositories.tasks.on_save = crash_after_second_completed  # type: ignore[attr-defined]
    try:
        RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    except RuntimeError:
        pass
    worker_dependencies.repositories.tasks.on_save = None  # type: ignore[attr-defined]
    calls_before = len(worker_dependencies.repositories.model_calls.list_by_request(req.message_id))
    worker_dependencies.repositories.messages.update_request_status("c1", req.message_id, "RUNNING", "ACCEPTED")  # operator requeue
    out = RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    assert out.started and worker_dependencies.repositories.messages.get("c1", req.message_id).status == "COMPLETED"
    executes_after = [c for c in worker_dependencies.repositories.model_calls.list_by_request(req.message_id) if c.stage == "execute"]
    assert len(executes_after) == 3 and len(worker_dependencies.repositories.model_calls.list_by_request(req.message_id)) == calls_before + 1
    assert len([m for m in worker_dependencies.repositories.messages.list_by_chat("c1") if m.kind == "report"]) == 1

def test_pending_interrupt_carries_next_action(tmp_path) -> None:
    db = tmp_path / "ckpt.sqlite"
    worker_dependencies = make_deps(ScriptedModel({"plan": [ModelResponse(structured=clarification("Which?"))]}))
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    RequestRunner(worker_dependencies, _saver(db)).start("c1", req.message_id)
    pending = RequestRunner(worker_dependencies, _saver(db)).pending_interrupt(req.message_id)
    assert pending is not None and pending["kind"] == "clarification" and pending["next_action"]
```

Add `clarification(question: str) -> dict` to `tests/helpers.py` (a `PlannerOutput` of kind clarification with `nextAction="wait for the answer"`).

The crash test: the task whose save raised is COMPLETED in the repository but has no checkpointed result; on the requeued `start` its execute task re-runs, hits `StaleStatusError` on `PLANNED -> RUNNING`, and returns the stored outcome without a model call. The first completed task is restored from the checkpoint. Only the third task pays a new execute call, so the total grows by exactly one.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.worker.runner`.

- [ ] **Step 3: Implement `runner.py`**

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py -v && uv run mypy src`
Expected: 6 passed, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/worker/runner.py tests/helpers.py tests/test_runner.py
git commit -m "feat: runner with SQLite checkpoints, clarification resume, crash restart, duplicate protection"
```

---

### Task 11: Compaction levels 1 and 3

**Files:**
- Create: `src/contextforge/core/context/compaction.py`
- Modify: `src/contextforge/worker/stages/execute.py` (call `compact` before each `call_model`)
- Test: `tests/test_compaction.py`

**Interfaces:**
- Consumes: `ModelClient`, `estimate_tokens`, `load_prompt("compress.1")`.
- Produces:
  - `compaction.stub_old_tool_results(messages: list[AnyMessage], keep_turns: int = 2) -> list[AnyMessage]`: every `ToolMessage` older than the last `keep_turns` AI turns has its content replaced by `f"[tool {name} args={args} outcome=ok rows={n} artifact={id or '-'}]"` using metadata the execute node stores in `ToolMessage.additional_kwargs` (`tool_name`, `arguments`, `row_count`, `artifact_id`).
  - `compaction.summarize_messages(messages, *, keep_last: int, model: ModelClient, memory: str) -> list[AnyMessage]`: finds the boundary at `len(messages) - keep_last`, moves it earlier while the message at the boundary is a `ToolMessage` (so a tool call and its result are never split), invokes `stage=compress` with the compress prompt and the older messages, and returns `[SystemMessage("Summary of earlier work: " + text)] + messages[boundary:]`.
  - `compaction.compact(messages, *, budget_tokens: int, keep_last: int, model, memory) -> list[AnyMessage]`: returns `messages` unchanged when their estimated tokens fit; else applies level 1; if still over, level 3; never calls the model when level 1 suffices.
  - `compaction.messages_tokens(messages) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compaction.py
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from contextforge.core.context.compaction import compact, stub_old_tool_results, summarize_messages
from contextforge.core.models.base import ModelResponse
from contextforge.core.models.scripted import ScriptedModel

def _tool(i: int, big: bool = False) -> ToolMessage:
    return ToolMessage(content=("x" * 4000) if big else "r", tool_call_id=f"t{i}",
        additional_kwargs={"tool_name": "sql_query", "arguments": {"sql": f"q{i}"}, "row_count": 3, "artifact_id": None})

def _turns(n: int, big: bool = False) -> list:
    out: list = [HumanMessage(content="task")]
    for i in range(n):
        out += [AIMessage(content="", tool_calls=[{"id": f"t{i}", "name": "sql_query", "args": {"sql": f"q{i}"}}]), _tool(i, big)]
    return out

def test_level1_stubs_only_old_tool_results() -> None:
    msgs = stub_old_tool_results(_turns(4, big=True), keep_turns=2)
    tools = [m for m in msgs if isinstance(m, ToolMessage)]
    assert tools[0].content.startswith("[tool sql_query") and tools[1].content.startswith("[tool")
    assert tools[2].content == "x" * 4000 and tools[3].content == "x" * 4000

def test_level3_never_splits_a_tool_call_from_its_result() -> None:
    model = ScriptedModel({"compress": [ModelResponse(text="did q0..q2")]})
    msgs = _turns(5)                      # 11 messages; keep_last=6 would land on a ToolMessage
    out = summarize_messages(msgs, keep_last=6, model=model, memory="M")
    assert out[0].content.startswith("Summary of earlier work")
    assert isinstance(out[1], AIMessage) and out[1].tool_calls
    assert len(out) == 1 + 7

def test_compact_uses_level1_without_model_when_enough() -> None:
    model = ScriptedModel({})             # any compress call would raise ScriptExhausted
    out = compact(_turns(4, big=True), budget_tokens=2500, keep_last=6, model=model, memory="M")
    assert any(isinstance(m, ToolMessage) and m.content.startswith("[tool") for m in out)

def test_level1_is_idempotent() -> None:
    once = stub_old_tool_results(_turns(4, big=True), keep_turns=2)
    twice = stub_old_tool_results(once, keep_turns=2)
    assert [m.content for m in once] == [m.content for m in twice]

def test_repeated_compaction_keeps_brief_fields_out_of_scope() -> None:
    # compaction only receives messages; brief fields are not passed and cannot be lost
    model = ScriptedModel({"compress": [ModelResponse(text="s")] * 3})
    msgs = _turns(12, big=True)
    for _ in range(3):
        msgs = compact(msgs, budget_tokens=1500, keep_last=6, model=model, memory="M")
        msgs += _turns(2, big=True)[1:]
    assert sum(1 for m in msgs if isinstance(m, AIMessage) and m.tool_calls) == \
           sum(1 for m in msgs if isinstance(m, ToolMessage))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_compaction.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.context.compaction`.

- [ ] **Step 3: Implement `compaction.py`; wire `compact` into `call_model` with `budget_tokens = budgets.messages_budget(built.fixed_tokens)`**

Also add an end-to-end check to `tests/test_workflow.py`: run the six-task fixture with a `Budgets` copy whose `cap_input_tokens` is 3,000 and a compress script of three responses; assert the request still COMPLETES and the report claims keep their evidence refs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check .`
Expected: all tests pass, clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/context/compaction.py src/contextforge/worker/stages/execute.py tests
git commit -m "feat: compaction levels 1 and 3 with tool-call boundary rule"
```

---

### Task 12: Phase close

**Files:**
- Modify: `docs/progress.md` (phase checklist and log), `README.md` (how to run tests)

- [ ] **Step 1: Run the full suite one more time**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: every test passes; no lint, format or type issues.

- [ ] **Step 2: Update `docs/progress.md`**

Phase 1 row becomes `Done <date>`; log entry lists what was verified (the five done criteria from implementation-plan section 13) and names the deviations recorded below.

- [ ] **Step 3: Commit**

```bash
git add docs/progress.md README.md
git commit -m "docs: phase 1 complete"
```

---

## Deviations from the spec, decided in this plan

- Packages live under `src/contextforge/` rather than as top-level `core`, `worker`, `api`.
- The phase 1 `sql_query` tool runs DuckDB in-process over the fixture CSV. The spec says "fake `sql_query` tool"; a real query over the fixture is simpler than scripting rows and still has no ingestion, isolation or limits, which stay in phase 3.
- Tasks run in dependency-driven waves with up to five in flight, as the grill decided; the spec's phase 1 row still says one subgraph at a time.
- `RequestState` carries `chat_id` and `user_id` in addition to the spec's fields, as identity for writes, and carries no `messages`: only `TaskState` has a transcript.
- `reconcile` is a stub that merges dependency summaries; `pdf_lookup` and `source_search` tasks are SKIPPED with a reason.
- The resume path appends the clarification answer to `constraints` and re-plans with the same planner call; a separate `clarify` stage is not introduced.
- Orchestration uses the Functional API (decided 5 November 2026) instead of the `await_user` node and the skip-on-replay task loop from the 4 November LLD review; deterministic ids and the cooperative cancel stay.
- Deadlines (60 s, 120 s, 15 min, per task kind) and the spend cap are not enforced in phase 1; they need the real model adapter and arrive with it in phase 2.
- The 7 November 2026 adoptions from the Cimba review that land in phase 1: tool error envelope, finish-now nudge, duplicate-call and no-progress detectors, "showing N of M" and shaping caps, byte-stability and idempotency tests, usage cache columns, price version, planner retry with errors, the context resolver. The rest (hypothesis status, code number check, trace assertions, constraints across requests, MCP pinning, memory gate) belong to phases 2 to 7 and are recorded in the design docs.
