# Phase 7: Memory — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every model call carries the user's standing instructions and capped learned memory in one byte-stable, versioned memory section; preferences shape the user turn and the resolver's timezone; claims and constraints carry across requests in a chat with their provenance; learned facts and episodes are written only by a promotion gate written in code; E14 (`GET`, `PATCH /me`) edits preferences and instructions.

**Architecture:** `User` and the new `AgentMemoryRecord` get repository Protocols with in-memory implementations (the DynamoDB adapters are phase 10's; keys are documented on the Protocols). A new `core/memory` package holds the credential guard, the preferences helpers, the memory policy, the memory-section renderer with its version hash, and the promotion gate. The workflow reads memory once, in its `load_brief` task, and keeps the snapshot in the checkpointed brief, so every call of a request (plan, execute, compress, cite) gets the same bytes and the same `context_version`, even across a clarification resume. The plan stage shows the claims of the chat's last two reports and the newest report's constraints with their source and date; code carries those constraints into the brief unless the planner drops one with a reason; the report records each constraint's origin and every drop. After a COMPLETED request the report stage hands the saved report to the promotion gate when `autoPromote` is on. E14 is two service functions behind phase 4's route.

**Tech Stack:** Python 3.12, `uv`, Pydantic 2, LangGraph 1.x (Functional API, SQLite checkpoints), FastAPI `TestClient` (phase 4), `zoneinfo`, `hashlib`, pytest, ruff, mypy strict. No new dependencies.

**Spec:** `docs/system-design.md` section 5 (memory layers, rules, promotion gate, constraints across requests, the `AgentMemoryRecord` envelope), section 6 (byte stability), section 14 row "7 Memory"; `docs/implementation-plan.md` 3.1 User, 3.3 `report.constraints`, 8.1, 8.2 (which stage sees memory and prior context), 8.7 (instructions and learned-fact rows), 10.1, 11.1 expectations 6 and 22, 11.3 and 11.4 E14; `evals/cases/golden-001.md` (phase 7 row); `.superpowers/milestone-2/brief.md`. Read them before any task.

## Global Constraints

- Python 3.12, `uv`. Before every commit: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, all clean.
- Tests alongside the code: few, clubbed, integration-focused (components connected), one test module per task. DRY; small single-purpose functions (about 40 lines at most); no duplicated logic blocks.
- Naming per implementation-plan 12.1: snake_case Python, camelCase on the wire and in YAML. Enum values exactly `semantic`, `episodic`, `candidate`, `active`, `superseded`, `deleted`. Entities extend `Entity` (camelCase aliases, `extra="forbid"`).
- `User.preferences` is `{ currency, timezone, locale }`, kept small; `User.instructions` is plain text capped at 2,000 characters; `userId` never comes from a client body (spec 3.1).
- `agent-memory.md` is developer-controlled and never model-writable; the model has no tool that writes any memory tier (system-design 5).
- The memory section is system block 1: `agent-memory.md`, then the user's instructions, then the capped learned records. `context_version` is `mem:<memoryVersion>|prompt:<promptVersion>`; `memoryVersion` is `MEMORY_VERSION` alone when the user part is empty, else `MEMORY_VERSION+<first 12 hex characters of sha256(user part)>`.
- Byte stability (system-design 6): both system blocks are byte-identical for every call of one request and change only with `context_version`; they hold no call timestamp, request id, resolved date or unsorted collection. Tested in Tasks 3 and 4.
- Credentials never enter any memory tier: instructions, learned records and carried constraints all pass one `contains_credential` guard; preferences are format-validated (system-design 5).
- Retrieval checks owner, `active` status and expiry in application code (system-design 5).
- Learned memory is written only by the promotion gate: only claims the cite stage CONFIRMED, carrying no uncertainty, stored with `sourceRefs` and `expiresAt`; every memory write names its reader (system-design 5). The gate runs only when `autoPromote` is true, and it ships false until compared against a no-episode baseline (Task 7).
- Concurrent updates need revision checks: a preferences or instructions update is one atomic merge; record writes are conditional on `revision` (system-design 5).
- The planner's prior context is the claims of the chat's last two reports, plus the constraints carried from the newest with source and date; the planner may drop one only with a stated reason (implementation-plan 8.2 row 8; system-design 5, "Constraints outlive a request").
- No network, AWS or paid model call in tests. The DynamoDB adapters for `users` and `memories` are phase 10's.
- Shared files touched additively: `core/repositories/memory.py` (`Repositories.users`, `Repositories.memories`), `worker/dependencies.py` (`memory_policy`; `model_for` gains a required `memory_version`), `tests/helpers.py`. New limits go in a new `config/memory.yaml`; `config/budgets.yaml` is not touched.
- Phases 2, 3 and 4 changed files this plan names (model wiring, the cite stage, scripted test scripts, the API). Read each file before editing it. Workflow tests below use the phase-1 script shape; after phase 3 they also script the cite responses, the way phase 3's own workflow tests do.

## Review Focus

1. Instructions edited or cleared while a request waits on a clarification: the resumed request keeps the memory it started with (same bytes, same `context_version`), and the next request uses the new text (Task 4).
2. A credential pasted into the instructions ("my key is AKIA…", "api_key=sk-…"): `PATCH /me` returns 400 `validation` and the stored instructions are unchanged (Task 1).
3. A long chat whose newest report lists 25 constraints, one of them credential-shaped: 20 carry, and every one that does not carry is listed with its reason in the new report's coverage; none vanish silently (Task 5).
4. The planner "drops" a carried constraint by paraphrase, or names one that was never carried: the drop is ignored and the carried constraint stays in force and in the report (Task 5).
5. The report task re-runs after a crash, a second request confirms the same fact, or two writers race: one record, refreshed rather than duplicated; a lost race is retried once and never fails the request (Task 6).

---

## File structure

```
config/memory.yaml                              memory policy: autoPromote, record caps, TTLs, carry cap (Task 2)
prompts/plan.2.md                               plan.1 plus the carried-constraint rule (Task 5)
evals/cases/golden-001-instructions.md          the phase 7 variant of the yardstick (Task 7)
src/contextforge/core/entities/agent_memory.py  AgentMemoryRecord, MemoryKind, MemoryStatus (Task 2)
src/contextforge/core/memory/__init__.py        package: everything about what is remembered
src/contextforge/core/memory/credentials.py     contains_credential (Task 1)
src/contextforge/core/memory/preferences.py     DEFAULT_TIMEZONE, is_timezone, user_timezone (Task 1)
src/contextforge/core/memory/policy.py          MemoryPolicy, load_memory_policy (Task 2)
src/contextforge/core/memory/section.py         select_records, render_user_memory, memory_version, build_memory_snapshot (Task 3)
src/contextforge/core/memory/gate.py            PromotionDecision, promote_report and its rules (Task 6)
src/contextforge/core/services/me.py            PreferencesPatch, MePatch, get_me, patch_me (Task 1)
src/contextforge/core/repositories/base.py      + UserRepository (Task 1), StaleRevisionError, AgentMemoryRepository (Task 2)
src/contextforge/core/repositories/memory.py    + InMemoryUsers (Task 1), InMemoryAgentMemory (Task 2); Repositories.users, .memories
src/contextforge/core/state.py                  + MemorySnapshot (Task 3), ConstraintOrigin, DroppedConstraint (Task 5); RequestState fields (Tasks 4, 5)
src/contextforge/core/planner.py                + ConstraintDrop, PlannerOutput.dropped_constraints (Task 5)
src/contextforge/core/context/builder.py        + ContextInputs.carried_constraints, rendered in prior_context (Task 5)
src/contextforge/core/context/prompts.py        plan stage -> plan.2 (Task 5)
src/contextforge/core/ids.py                    + plus_days (Task 6)
src/contextforge/worker/dependencies.py                 + memory_policy; model_for(..., *, memory_version) (Task 4)
src/contextforge/worker/request_state.py                + load_memory; create_initial_request_state(request, memory) (Tasks 4, 5)
src/contextforge/worker/workflow.py             load_brief reads memory; answers and carried constraints keep their origin (Tasks 4, 5)
src/contextforge/worker/stages/plan.py          memory, preferences, timezone from the brief; prior reports; carried and dropped (Tasks 4, 5)
src/contextforge/worker/stages/prior.py         prior_reports, carried_constraints, apply_drops (Task 5)
src/contextforge/worker/stages/execute.py       memory, preferences and memory version from the brief (Task 4)
src/contextforge/worker/stages/citation_checks.py          phase 3's cite call: memory and version from the brief (Task 4)
src/contextforge/worker/stages/report.py        report date in the user's timezone (Task 4); constraint origins and drops (Task 5); promotion (Task 6)
src/contextforge/worker/stages/promote.py       promote_completed_request (Task 6)
src/contextforge/api/…                          phase 4's E14 handlers call get_me / patch_me (Task 1)
tests/helpers.py                                + seed_user, memory_record, create_initial_request_state(memory=None), sqlite_saver, reply_to, claim, seed_report, make_deps(auto_promote=)
tests/test_me.py, test_agent_memory.py, test_memory_section.py, test_memory_in_calls.py,
tests/test_constraints_across_requests.py, test_promotion_gate.py
```

---

### Task 1: Editable preferences and instructions (`PATCH /me`)

**Files:**
- Create: `src/contextforge/core/memory/__init__.py`, `src/contextforge/core/memory/credentials.py`, `src/contextforge/core/memory/preferences.py`
- Modify: `src/contextforge/core/repositories/base.py` (`UserRepository` gains `update_preferences_and_instructions`), `src/contextforge/core/repositories/memory.py` (`InMemoryUsers` implements it), `src/contextforge/core/services/users.py` (phase 4's module: `PreferencesPatch`, `MePatch`, `patch_me`), `src/contextforge/api/routes/me.py` (`PATCH /me`), `tests/helpers.py` (`seed_user`)
- Test: `tests/test_me.py`

**Interfaces:**
- Consumes: `User`, `INSTRUCTIONS_MAX_CHARS` (`core/entities/user.py`); `Entity`; `now_iso`. From phase 4: `UserRepository` (`get`, `save`), `InMemoryUsers`, `Repositories.users`, `users.ensure_user(repos, *, user_id, email)`, `users.get_user(repos, user_id)` (raises `NotFound`), `routes/me.py` with `GET /me`, `ContainerDep`, `UserIdDep`, the handler that turns `RequestValidationError` into 400 `validation`, and `tests/api_support.api_client(tmp_path)`, a context manager yielding `(TestClient, Container)` signed in as `DEV_USER`.
- Produces:
  - `credentials.contains_credential(text: str) -> bool`: true when any of these match: `\b(?:AKIA|ASIA)[0-9A-Z]{16}\b`; `-----BEGIN [A-Z ]*PRIVATE KEY-----`; `(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|token)\s*[:=]\s*\S+`; `\bsk-[A-Za-z0-9_-]{20,}`; `\bgh[pousr]_[A-Za-z0-9]{36}\b`; `\bxox[abprs]-[A-Za-z0-9-]{10,}`; `\blsv2_[a-z]{2}_[A-Za-z0-9_]{20,}`; `\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`. A guard against common shapes, not a guarantee (say so in the docstring); the one list every memory writer uses.
  - `preferences.DEFAULT_TIMEZONE = "Asia/Kolkata"`; `preferences.is_timezone(name: str) -> bool` (`ZoneInfo(name)` loads; `ZoneInfoNotFoundError` and `ValueError` give False); `preferences.user_timezone(preferences: Mapping[str, str]) -> str` (the stored `timezone` when it loads, else the default).
  - `UserRepository.update_preferences_and_instructions(user_id: str, *, preferences: Mapping[str, str] | None = None, instructions: str | None = None) -> User`: one atomic write that merges `preferences` key by key into the stored map (an absent map counts as empty) and replaces `instructions` (`""` clears them to None); None leaves a field unchanged; `updated_at` moves; `LookupError` when the user does not exist. The updated User is built with `User.model_validate`, not `model_copy`, so the entity's 2,000-character cap holds here too. Two edits of different fields never overwrite each other. The docstring records the phase 10 shape: item `USER#<userId>` / `PROFILE`, one `UpdateItem` with `SET preferences.#k = :v` per key (creating the map first when absent) and `attribute_exists(PK)`.
  - `users.PreferencesPatch(Entity)`: `currency: str | None = Field(None, pattern=r"^[A-Z]{3}$")`, `timezone: str | None = None` (validator: `is_timezone`), `locale: str | None = Field(None, pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")`; unknown keys are refused by `Entity`.
  - `users.MePatch(Entity)`: `preferences: PreferencesPatch | None = None`, `instructions: str | None = Field(None, max_length=INSTRUCTIONS_MAX_CHARS)`. A `mode="before"` validator normalises instructions (`\r\n` to `\n`, then strip); an after-validator refuses text where `contains_credential` is true; a model validator requires at least one field. It is both the route's body type and the service's input, so the rules live in `core` and FastAPI enforces them at the edge.
  - `users.patch_me(repos: Repositories, *, user_id: str, patch: MePatch) -> User`: `get_user` (so an unknown user is `NotFound`), then one `update_preferences_and_instructions` with `patch.preferences.model_dump(exclude_none=True)` (None when absent) and `patch.instructions`. The read side of E14 stays phase 4's `get_user`; there is no second getter.
  - `routes/me.py`: `PATCH /me` with body `MePatch`, the user from `UserIdDep` (never the body), returns 200 `User`.
  - `tests/helpers.seed_user(deps: WorkerDependencies, *, user_id: str, instructions: str | None = None, preferences: dict[str, str] | None = None) -> User`: saves through `deps.repos.users.save`, bypassing `MePatch`, so a test can store a value validation would refuse.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_me.py
from pathlib import Path

import pytest
from pydantic import ValidationError

from contextforge.core.entities.user import User
from contextforge.core.memory.credentials import contains_credential
from contextforge.core.repositories.memory import Repositories
from contextforge.core.services.users import MePatch, ensure_user, patch_me
from tests.api_support import api_client

INSTRUCTION = "Always exclude transfers unless I say otherwise"


def _patch(repositories: Repositories, body: dict) -> User:
    return patch_me(repositories, user_id="u1", patch=MePatch.model_validate(body))


def test_patch_merges_preferences_and_replaces_instructions() -> None:
    repositories = Repositories.in_memory()
    ensure_user(repositories, user_id="u1", email="u1@example.com")
    _patch(repositories, {"preferences": {"currency": "INR", "timezone": "Asia/Kolkata"}})
    user = _patch(repositories, {"preferences": {"currency": "USD"}, "instructions": f"  {INSTRUCTION}\r\n"})
    assert user.preferences == {"currency": "USD", "timezone": "Asia/Kolkata"}
    assert user.instructions == INSTRUCTION
    cleared = _patch(repositories, {"instructions": ""})
    assert cleared.instructions is None and cleared.preferences == user.preferences


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"instructions": "x" * 2001},
        {"instructions": "My AWS key is AKIAABCDEFGHIJKLMNOP, use it"},
        {"instructions": "password: hunter2"},
        {"preferences": {"timezone": "Mars/Olympus"}},
        {"preferences": {"currency": "rupees"}},
        {"preferences": {"theme": "dark"}},
    ],
)
def test_an_invalid_patch_is_refused(body: dict) -> None:
    with pytest.raises(ValidationError):
        MePatch.model_validate(body)


def test_credential_shapes() -> None:
    assert contains_credential("token=ghp_" + "a" * 36)
    assert contains_credential("-----BEGIN RSA PRIVATE KEY-----")
    assert not contains_credential(INSTRUCTION)
    assert not contains_credential("A card purchase and its bill payment count once.")


def test_patch_me_over_http(tmp_path: Path) -> None:
    with api_client(tmp_path) as (client, _):
        ok = client.patch("/me", json={"preferences": {"currency": "INR"}, "instructions": INSTRUCTION})
        bad = client.patch("/me", json={"instructions": "api_key=sk-" + "a" * 24})
        me = client.get("/me").json()
    assert ok.status_code == 200 and ok.json()["instructions"] == INSTRUCTION
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "validation"
    assert me["preferences"] == {"currency": "INR"} and me["instructions"] == INSTRUCTION
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_me.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.memory`.

- [ ] **Step 3: Implement the modules and the method in Interfaces, then add `PATCH /me`**

The route returns the User the way phase 4's `GET /me` does (camelCase).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_me.py -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: 10 passed; clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/memory src/contextforge/core/services/users.py src/contextforge/core/repositories src/contextforge/api/routes/me.py tests/helpers.py tests/test_me.py
git commit -m "feat: PATCH /me edits preferences and instructions behind a credential guard"
```

---

### Task 2: AgentMemoryRecord, its repository and the memory policy

**Files:**
- Create: `src/contextforge/core/entities/agent_memory.py`, `src/contextforge/core/memory/policy.py`, `config/memory.yaml`
- Modify: `src/contextforge/core/repositories/base.py` (`StaleRevisionError`, `AgentMemoryRepository`), `src/contextforge/core/repositories/memory.py` (`InMemoryAgentMemory`; `Repositories.memories`), `tests/helpers.py` (`memory_record`)
- Test: `tests/test_agent_memory.py`

**Interfaces:**
- Consumes: Task 1 `contains_credential`; `EvidenceRef`, `Locator`, `LocatorKind`; `Entity`.
- Produces:
  - `MemoryKind(StrEnum)`: `semantic`, `episodic`. `MemoryStatus(StrEnum)`: `candidate`, `active`, `superseded`, `deleted`.
  - `AgentMemoryRecord(Entity)`: `memory_id: str`, `user_id: str`, `kind: MemoryKind`, `content: str` (min length 1; a validator refuses `contains_credential`), `source_refs: list[EvidenceRef]` (min length 1: provenance is required), `revision: int = 0`, `status: MemoryStatus`, `created_at: str`, `updated_at: str`, `last_verified_at: str`, `expires_at: str`.
  - `base.StaleRevisionError(Exception)` with attributes `expected: int | None` and `actual: int | None`.
  - `base.AgentMemoryRepository` Protocol: `get(user_id: str, memory_id: str) -> AgentMemoryRecord | None`; `list_by_user(user_id: str) -> list[AgentMemoryRecord]` (every status, ordered by `memory_id`); `put(record: AgentMemoryRecord, *, expected_revision: int | None) -> AgentMemoryRecord`: stores `record` with `revision = (expected_revision or 0) + 1` and returns the stored copy; `expected_revision=None` requires that no record exists, otherwise the stored revision must equal it; anything else raises `StaleRevisionError(expected, actual)`. The docstring records the phase 10 shape: item `USER#<userId>` / `MEM#<memoryId>`, `PutItem` conditioned on `attribute_not_exists(SK)` or `revision = :expected`.
  - `memory.InMemoryAgentMemory`; `Repositories.memories: AgentMemoryRepository`, filled by `in_memory()`.
  - `policy.MemoryPolicy(Entity)` with `model_config = ConfigDict(frozen=True, strict=True)` (strict, so `true` is not an int and `"8"` is not a number): `auto_promote: bool`, `records_cap: int`, `records_max_chars: int`, `record_max_chars: int`, `semantic_ttl_days: int`, `episodic_ttl_days: int`, `carried_constraints_cap: int`; method `ttl_days(kind: MemoryKind) -> int`. `policy.MEMORY_POLICY_PATH` resolved from the module file the way `BUDGETS_PATH` is; `policy.load_memory_policy(path: Path = MEMORY_POLICY_PATH) -> MemoryPolicy` = `model_validate(yaml.safe_load(...))`, the `config/models.yaml` pattern.
  - `config/memory.yaml`, each key with a one-line comment: `autoPromote: false`, `recordsCap: 8`, `recordsMaxChars: 1200`, `recordMaxChars: 300`, `semanticTtlDays: 90`, `episodicTtlDays: 365`, `carriedConstraintsCap: 20`.
  - `tests/helpers.memory_record(memory_id: str, content: str, *, user_id: str = "u1", kind: str = "semantic", status: str = "active", verified: str = "2026-11-06T09:00:00Z", expires: str = "2027-02-04T09:00:00Z") -> AgentMemoryRecord` with one source ref, artifact `a2`, locator row `12,13`, and `created_at = updated_at = last_verified_at = verified`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_memory.py
import pytest
from pydantic import ValidationError

from contextforge.core.entities.agent_memory import AgentMemoryRecord, MemoryKind
from contextforge.core.memory.policy import load_memory_policy
from contextforge.core.repositories.base import StaleRevisionError
from contextforge.core.repositories.memory import InMemoryAgentMemory
from tests.helpers import memory_record


def test_put_is_conditional_on_revision() -> None:
    repo = InMemoryAgentMemory()
    record = memory_record("k1", "ACC-SAV-1 and ACC-CUR-2 are the user's own accounts.")
    first = repo.put(record, expected_revision=None)
    assert first.revision == 1 and repo.get("u1", "k1") == first
    with pytest.raises(StaleRevisionError):
        repo.put(record, expected_revision=None)  # it exists now
    refreshed = first.model_copy(update={"last_verified_at": "2026-11-07T09:00:00Z"})
    assert repo.put(refreshed, expected_revision=1).revision == 2
    with pytest.raises(StaleRevisionError) as stale:
        repo.put(first, expected_revision=1)  # a writer that read revision 1
    assert (stale.value.expected, stale.value.actual) == (1, 2)
    assert [r.memory_id for r in repo.list_by_user("u1")] == ["k1"] and repo.list_by_user("u2") == []


def test_a_record_needs_provenance_and_refuses_credentials() -> None:
    record = memory_record("k1", "Netflix rose from 649 to 799 in October.")
    assert record.model_dump(by_alias=True)["sourceRefs"][0]["artifactId"] == "a2"
    with pytest.raises(ValidationError):
        AgentMemoryRecord.model_validate({**record.model_dump(), "source_refs": []})
    with pytest.raises(ValidationError):
        memory_record("k2", "The user's password: hunter2")


def test_memory_policy_values() -> None:
    p = load_memory_policy()
    assert (p.auto_promote, p.records_cap, p.records_max_chars, p.record_max_chars) == (False, 8, 1200, 300)
    assert (p.ttl_days(MemoryKind.SEMANTIC), p.ttl_days(MemoryKind.EPISODIC)) == (90, 365)
    assert p.carried_constraints_cap == 20
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_agent_memory.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.entities.agent_memory`.

- [ ] **Step 3: Implement the entity, the repository, the policy and `config/memory.yaml`**

The entity follows the system-design 5 envelope minus `tags` and `artifactRef`, which nothing reads yet (Deviations).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_agent_memory.py -v && uv run mypy src && uv run ruff check .`
Expected: 3 passed; clean.

- [ ] **Step 5: Commit**

```bash
git add config/memory.yaml src/contextforge/core tests/helpers.py tests/test_agent_memory.py
git commit -m "feat: AgentMemoryRecord with revision-checked repository and memory policy"
```

---

### Task 3: The memory section: instructions and learned records, capped and versioned

**Files:**
- Create: `src/contextforge/core/memory/section.py`
- Modify: `src/contextforge/core/state.py` (`MemorySnapshot`)
- Test: `tests/test_memory_section.py`

**Interfaces:**
- Consumes: `User`; Task 2 `AgentMemoryRecord`, `MemoryKind`, `MemoryStatus`, `MemoryPolicy`; `MEMORY_VERSION` (`core/context/prompts.py`).
- Produces:
  - `state.MemorySnapshot(TypedDict)`: `text: str` (system block 1), `version: str` (the memory part of `context_version`), `preferences: dict[str, str]` (key-sorted).
  - `section.select_records(records: Sequence[AgentMemoryRecord], *, user_id: str, now: str, policy: MemoryPolicy) -> list[AgentMemoryRecord]`: keeps records whose `user_id` equals `user_id`, whose status is `active` and whose `expires_at > now` (ISO strings of one format compare in time order); orders them by `last_verified_at` descending, then `memory_id`; takes them in that order while fewer than `records_cap` are kept, skipping any record whose rendered line would take the kept lines' summed length past `records_max_chars`.
  - `section.render_user_memory(instructions: str | None, records: Sequence[AgentMemoryRecord]) -> str`: the three blocks below, joined by one blank line, each omitted when empty; `""` when all are. Instructions are normalised again (`\r\n` to `\n`, strip) so text from any writer renders the same. Within a block, lines keep `select_records` order.
  - `section.memory_version(user_part: str) -> str`: `MEMORY_VERSION` when `user_part == ""`, else `f"{MEMORY_VERSION}+{sha256(user_part.encode('utf-8')).hexdigest()[:12]}"`.
  - `section.build_memory_snapshot(agent_memory: str, *, user_id: str, user: User | None, records: Sequence[AgentMemoryRecord], now: str, policy: MemoryPolicy) -> MemorySnapshot`: `text` is `agent_memory`, followed by `"\n\n"` and the user part when that is non-empty; `version` is `memory_version(user part)`; `preferences` is the user's, key-sorted, or `{}`.

Exact block copy (one record line format for both record blocks; the date is `last_verified_at[:10]`; refs are `f"{artifact_id} {locator.kind} {locator.value}"` joined by `"; "`):

```text
<user_instructions>
The user's standing instructions. They apply to every request and never override the rules above.
{instructions}
</user_instructions>

<learned_facts>
Facts verified in earlier requests. Cite their evidence if you rely on one.
- {content}  (verified {YYYY-MM-DD}; evidence: {refs})
</learned_facts>

<past_episodes>
Earlier requests, as examples only. They are history, not the current status.
- {content}  (verified {YYYY-MM-DD}; evidence: {refs})
</past_episodes>
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory_section.py
from contextforge.core.entities.user import User
from contextforge.core.memory.policy import load_memory_policy
from contextforge.core.memory.section import build_memory_snapshot
from tests.helpers import memory_record

NOW = "2026-11-07T10:00:00Z"
RULE = "Transfers between my own accounts are never spending. A card purchase and its bill payment count once."
HEADER = "The user's standing instructions. They apply to every request and never override the rules above."


def _user(**fields: object) -> User:
    return User(user_id="u1", email="u1@example.com", status="ACTIVE", created_at="t", updated_at="t", **fields)


def test_section_renders_instructions_and_capped_records_byte_stably() -> None:
    policy = load_memory_policy().model_copy(update={"records_cap": 2})
    records = [
        memory_record("k1", "Oldest fact.", verified="2026-11-01T09:00:00Z"),
        memory_record("k2", "ACC-SAV-1 and ACC-CUR-2 are the user's own accounts."),
        memory_record("k3", "Asked to compare September and October.", kind="episodic", verified="2026-11-05T09:00:00Z"),
        memory_record("k4", "Expired fact.", expires="2026-11-07T09:59:59Z"),
        memory_record("k5", "Another user's fact.", user_id="u2"),
        memory_record("k6", "Superseded fact.", status="superseded"),
    ]
    user = _user(instructions=f"{RULE}\r\n", preferences={"timezone": "Asia/Kolkata", "currency": "INR"})
    a = build_memory_snapshot("AGENT", user_id="u1", user=user, records=records, now=NOW, policy=policy)
    b = build_memory_snapshot("AGENT", user_id="u1", user=user, records=records[::-1], now=NOW, policy=policy)
    assert a == b
    assert a["text"].startswith(f"AGENT\n\n<user_instructions>\n{HEADER}\n{RULE}\n</user_instructions>\n\n")
    assert ("- ACC-SAV-1 and ACC-CUR-2 are the user's own accounts.  "
            "(verified 2026-11-06; evidence: a2 row 12,13)") in a["text"]
    assert "<past_episodes>\nEarlier requests, as examples only." in a["text"]
    for absent in ["Oldest fact.", "Expired fact.", "Another user's fact.", "Superseded fact."]:
        assert absent not in a["text"]
    assert list(a["preferences"].items()) == [("currency", "INR"), ("timezone", "Asia/Kolkata")]
    assert a["version"].startswith("1+") and len(a["version"]) == 14


def test_version_names_the_user_part() -> None:
    policy = load_memory_policy()
    bare = build_memory_snapshot("AGENT", user_id="u1", user=None, records=[], now=NOW, policy=policy)
    assert bare == {"text": "AGENT", "version": "1", "preferences": {}}
    versions = [
        build_memory_snapshot("AGENT", user_id="u1", user=_user(instructions=text), records=[], now=NOW, policy=policy)["version"]
        for text in [RULE, RULE + "\n", "Report amounts in INR."]
    ]
    assert versions[0] == versions[1] != versions[2] and "1" not in versions


def test_char_cap_skips_a_record_that_would_overflow() -> None:
    policy = load_memory_policy().model_copy(update={"records_max_chars": 120})
    records = [memory_record("k1", "x" * 90), memory_record("k2", "Short fact.", verified="2026-11-05T09:00:00Z")]
    text = build_memory_snapshot("AGENT", user_id="u1", user=None, records=records, now=NOW, policy=policy)["text"]
    assert "x" * 90 not in text and "Short fact." in text  # the 139-character line would pass 120
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_memory_section.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.memory.section`.

- [ ] **Step 3: Implement `section.py` and `MemorySnapshot`**

A pure module: no clock, no repository; `now` is passed in.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_memory_section.py -v && uv run mypy src && uv run ruff check .`
Expected: 3 passed; clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge/core/memory/section.py src/contextforge/core/state.py tests/test_memory_section.py
git commit -m "feat: memory section with instructions, capped learned records and a content version"
```

---

### Task 4: Memory in every call: the request-start snapshot, preferences and timezone

**Files:**
- Modify: `src/contextforge/core/state.py` (`RequestState.memory`), `src/contextforge/worker/dependencies.py`, `src/contextforge/worker/request_state.py`, `src/contextforge/worker/workflow.py` (`load_initial_request_state`), `src/contextforge/worker/stages/plan.py`, `src/contextforge/worker/stages/execute.py`, `src/contextforge/worker/stages/citation_checks.py` (phase 3's model call), `src/contextforge/worker/stages/report.py` (`_constraints` date), `tests/helpers.py` (`create_initial_request_state`, `sqlite_saver`, `reply_to`), `tests/test_runner.py` (use `sqlite_saver` and `reply_to` instead of its private `_saver` and `_reply`)
- Test: `tests/test_memory_in_calls.py`

**Interfaces:**
- Consumes: Task 1 `UserRepository`, `user_timezone`; Task 2 `AgentMemoryRepository`, `MemoryPolicy`, `load_memory_policy`; Task 3 `build_memory_snapshot`, `MemorySnapshot`; `load_prompt`.
- Produces:
  - `RequestState.memory: MemorySnapshot`. Read once, at request start, inside the `load_brief` task, and checkpointed with its result: a resumed request runs under the memory it started with, and an edit applies from the next request.
  - `WorkerDependencies.memory_policy: MemoryPolicy = field(default_factory=load_memory_policy)` (after the defaulted fields, so existing constructors keep working); `WorkerDependencies.model_for(request_message_id: str, *, memory_version: str) -> ModelClient`, no default, so every caller passes `brief["memory"]["version"]` (if phase 2 reshaped `model_for`, keep its shape and add the keyword).
  - `brief.load_memory(repos: Repositories, user_id: str, policy: MemoryPolicy, *, now: str) -> MemorySnapshot` = `build_memory_snapshot(load_prompt("agent-memory"), user_id=user_id, user=repos.users.get(user_id), records=repos.memories.list_by_user(user_id), now=now, policy=policy)`. After this task it is the only caller of `load_prompt("agent-memory")`.
  - `brief.create_initial_request_state(request: RequestMessage, memory: MemorySnapshot) -> RequestState`; `workflow.load_initial_request_state` passes `load_memory(deps.repos, request.user_id, deps.memory_policy, now=now_iso())` (a clock read inside a task, which the workflow allows).
  - `plan.local_today(timezone: str) -> date`; `USER_TIMEZONE` is deleted. The resolver, the planner's `<context>` block and the report's constraint dates use `user_timezone(brief["memory"]["preferences"])`.
  - Every `ContextInputs` a stage builds (plan, execute, and phase 3's cite) takes `memory=brief["memory"]["text"]` and `preferences=brief["memory"]["preferences"]`; every `model_for` call passes `memory_version=brief["memory"]["version"]` (execute reads the brief from `TaskContext.brief`). Compaction already reuses `built.system[0]`, so compress calls inherit the same block.
  - `tests/helpers.create_initial_request_state(request: RequestMessage, memory: MemorySnapshot | None = None) -> RequestState` (None: `load_memory` over `Repositories.in_memory()`, which is `agent-memory.md` alone); `tests/helpers.sqlite_saver(path: Path | str) -> SqliteSaver`; `tests/helpers.reply_to(request: RequestMessage, target: str, content: str = "ACC-SAV-1 and ACC-CUR-2", message_id: str = "m3") -> ClarificationResponseMessage`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_memory_in_calls.py
from datetime import date

from contextforge.core.memory.preferences import DEFAULT_TIMEZONE, user_timezone
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.services.submit import submit_request
from contextforge.worker.runner import RequestRunner
from contextforge.worker.stages.plan import create_research_plan
from contextforge.worker.workflow import load_initial_request_state
from tests.helpers import (
    clarification, make_deps, reply_to, seed_chat_with_dataset, seed_user, six_task_plan,
    sqlite_saver, t1_result,
)

OLD = "Transfers between my own accounts are never spending."
NEW = "Treat transfers to ACC-CUR-2 as spending."


def _recording(script: dict) -> tuple[ScriptedModel, list[ModelRequest]]:
    seen: list[ModelRequest] = []
    model = ScriptedModel(script)
    model.on_request = seen.append
    return model, seen


def test_every_call_of_a_request_shares_one_memory_snapshot() -> None:
    model, seen = _recording({"plan": [ModelResponse(structured=six_task_plan())],
                              "execute": [ModelResponse(structured=t1_result())] * 3})
    worker_dependencies = make_deps(model, tasks_in_flight=1)
    seed_user(worker_dependencies, user_id="u1", instructions=OLD,
              preferences={"timezone": "America/New_York", "currency": "USD"})
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    assert RequestRunner(worker_dependencies, sqlite_saver(":memory:")).start("c1", req.message_id).status == "COMPLETED"
    assert {r.stage for r in seen} >= {"plan", "execute"}
    assert len({r.system[0] for r in seen}) == 1 and OLD in seen[0].system[0]
    calls = worker_dependencies.repositories.model_calls.list_by_request(req.message_id)
    memory_parts = {c.context_version.split("|")[0] for c in calls}
    assert len(memory_parts) == 1 and memory_parts.pop().startswith("mem:1+")
    plan_turn = next(r for r in seen if r.stage == "plan").messages[0].content
    assert "<user_preferences>currency: USD   timezone: America/New_York</user_preferences>" in plan_turn
    assert "(America/New_York)" in plan_turn


def test_memory_edited_while_waiting_applies_from_the_next_request(tmp_path) -> None:
    model, seen = _recording({"plan": [ModelResponse(structured=clarification("Which accounts are yours?")),
                                       ModelResponse(structured=six_task_plan()),
                                       ModelResponse(structured=six_task_plan())],
                              "execute": [ModelResponse(structured=t1_result())] * 6})
    worker_dependencies = make_deps(model, tasks_in_flight=1)
    seed_user(worker_dependencies, user_id="u1", instructions=OLD)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    db = tmp_path / "ckpt.sqlite"
    RequestRunner(worker_dependencies, sqlite_saver(db)).start("c1", req.message_id)
    worker_dependencies.repositories.users.update_preferences_and_instructions("u1", instructions=NEW)
    pending = worker_dependencies.repositories.messages.pending_for_request("c1", req.message_id)[0]
    RequestRunner(worker_dependencies, sqlite_saver(db)).resume("c1", req.message_id, reply_to(req, pending.message_id))
    assert seen and all(OLD in r.system[0] and NEW not in r.system[0] for r in seen)
    seen.clear()
    later = submit_request(worker_dependencies.repositories, client_key="k2", chat_id="c1", user_id="u1", content="And November?")
    RequestRunner(worker_dependencies, sqlite_saver(db)).start("c1", later.message_id)
    assert seen and all(NEW in r.system[0] and OLD not in r.system[0] for r in seen)


def test_a_stored_timezone_that_does_not_load_falls_back() -> None:
    assert user_timezone({}) == DEFAULT_TIMEZONE == "Asia/Kolkata"
    assert user_timezone({"timezone": "Europe/London"}) == "Europe/London"
    assert user_timezone({"timezone": "Mars/Olympus"}) == DEFAULT_TIMEZONE
    model, seen = _recording({"plan": [ModelResponse(structured=six_task_plan())]})
    worker_dependencies = make_deps(model)
    seed_user(worker_dependencies, user_id="u1", preferences={"timezone": "Mars/Olympus"})  # stored past validation
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    request_state = load_initial_request_state(worker_dependencies, "c1", req.message_id)
    assert create_research_plan(request_state, 0, worker_dependencies, today=date(2026, 11, 7))["kind"] == "plan"
    assert "today: 2026-11-07 (Asia/Kolkata)" in seen[0].messages[0].content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_memory_in_calls.py -v`
Expected: FAIL; `ImportError` for `sqlite_saver` from `tests.helpers`, then failed assertions once the helpers exist.

- [ ] **Step 3: Thread the snapshot through the brief, the stages and `model_for`**

Afterwards `grep -rn 'agent-memory\|model_for(' src` shows `brief.load_memory` as the only memory loader and every `model_for` call with `memory_version=`. Execute and cite now show `<user_preferences>` with the user's values (spec 8.2: both see section 2).

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: every test passes (the phase 1 plan-stage tests get the default snapshot through `tests.helpers.create_initial_request_state`); clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge tests
git commit -m "feat: memory snapshot read at request start and sent with every call"
```

---

### Task 5: Constraints across requests and the planner's prior context

**Files:**
- Create: `src/contextforge/worker/stages/prior.py`, `prompts/plan.2.md`
- Modify: `src/contextforge/core/state.py`, `src/contextforge/core/planner.py`, `src/contextforge/core/context/builder.py`, `src/contextforge/core/context/prompts.py`, `src/contextforge/worker/request_state.py`, `src/contextforge/worker/stages/plan.py`, `src/contextforge/worker/workflow.py`, `src/contextforge/worker/stages/report.py`, `tests/helpers.py` (`claim`, `seed_report`), `tests/test_models.py` (its expected `context_version` becomes `mem:1|prompt:plan.2`)
- Test: `tests/test_constraints_across_requests.py`

**Interfaces:**
- Consumes: Task 4 `brief["memory"]`, `user_timezone`, `local_today`; Task 2 `MemoryPolicy.carried_constraints_cap`; Task 1 `contains_credential`; `ReportConstraint`, `ReportMessage`, `MessageRepository`, `derived_id`.
- Produces:
  - `state.ConstraintOrigin(TypedDict)`: `source: str` (`"request"`, or the clarification_response message id), `date: str | None` (ISO date first stated; None until the report stamps it). `state.DroppedConstraint(TypedDict)`: `text: str`, `reason: str`.
  - `RequestState.constraint_origins: dict[str, ConstraintOrigin]` (keyed by constraint text; a constraint with no entry came from this request) and `RequestState.dropped_constraints: list[DroppedConstraint]`; `create_initial_request_state` sets `{}` and `[]`.
  - `planner.ConstraintDrop(Entity)`: `text: str`, `reason: str = Field(min_length=1)`; `PlannerOutput.dropped_constraints: list[ConstraintDrop] = []` (wire `droppedConstraints`; an empty reason fails the schema and takes the existing one-retry path).
  - `builder.ContextInputs.carried_constraints: Sequence[ReportConstraint] = ()`, rendered at the end of `<prior_context>` after the reports' claims: the line `constraints in force from earlier requests:`, then one line per constraint, `f"- {text}  (source: {label}, {date})"`, where `label` is `request` or `f"clarification answer {source}"`. Nothing is rendered when empty.
  - `prior.PRIOR_REPORTS = 2`; `prior.prior_reports(messages: MessageRepository, request: RequestMessage, limit: int = PRIOR_REPORTS) -> list[ReportMessage]`: the reports in the request's chat whose `request_message_id` sorts before `request.message_id` (ULIDs, so earlier requests), ordered by it, the last `limit`.
  - `prior.carried_constraints(reports: Sequence[ReportMessage], *, cap: int) -> tuple[list[ReportConstraint], list[DroppedConstraint]]`: walks the newest report's constraints in order; a credential-shaped one is dropped with reason `"looks like a credential"`; once `cap` are kept, the rest are dropped with `f"more than {cap} constraints carried"`. No reports gives `([], [])`. Only the newest report is read: it already re-recorded what it carried.
  - `prior.apply_drops(carried: Sequence[ReportConstraint], drops: Sequence[ConstraintDrop]) -> tuple[list[ReportConstraint], list[DroppedConstraint]]`: a drop whose text equals a carried constraint's removes it, keeping the planner's reason; a drop naming nothing carried is logged at warning and ignored.
  - `PlanResult.carried: dict[str, ConstraintOrigin]` (in force, in order) and `PlanResult.dropped: list[DroppedConstraint]` (cap and credential drops, then the planner's). The planner's `ContextInputs` get `prior_messages=prior_reports(...)` (claims only, through the existing `_prior`) and `carried_constraints=` the capped list; the old "every other message" selection is deleted.
  - Workflow: each clarification answer adds `f"User answered '{question}': {content}"` with origin `{"source": answer["message_id"], "date": None}`; `_with_plan` sets constraints to the unique union of carried, the brief's (answers) and the planner's, in that order; origins to `{**brief["constraint_origins"], **planned["carried"]}` (a constraint keeps the origin it was first stated with); `dropped_constraints` to `planned["dropped"]`.
  - Report: each `report.constraints` entry takes its origin's source and date (None becomes the report date in the user's timezone); without an origin, `source="request"` and that date. `coverage["droppedConstraints"]` is the brief's list when non-empty.
  - `tests/helpers.claim(claim_id: str, text: str, *, status: str = "CONFIRMED", refs: list[EvidenceRef] | None = None) -> Claim` (None gives one ref, `a2` row `12,13`; `[]` stays empty); `tests/helpers.seed_report(deps: WorkerDependencies, *, chat_id: str, user_id: str, claims: list[Claim], constraints: list[ReportConstraint] | None = None, uncertainties: list[str] | None = None, request_status: RequestStatus = RequestStatus.COMPLETED) -> tuple[RequestMessage, ReportMessage]`: saves an earlier request (content `"Earlier request."`, new ULID) and its report with id `derived_id(f"{request_id}:report")`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_constraints_across_requests.py
from contextforge.core.entities.message import ReportConstraint
from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.planner import ConstraintDrop
from contextforge.worker.runner import RequestRunner
from contextforge.worker.stages.plan import create_research_plan
from contextforge.worker.stages.prior import apply_drops, carried_constraints
from contextforge.worker.workflow import load_initial_request_state
from tests.helpers import (
    claim, clarification, make_deps, reply_to, seed_chat_with_dataset, seed_report, six_task_plan,
    sqlite_saver, t1_result,
)

EARLIER = "2026-11-01"
TEST_ACCOUNTS = ReportConstraint(text="Exclude account ACC-TEST-9.", source="request", date=EARLIER)
OLD_PERIOD = ReportConstraint(text="Period: 2026-09-01 to 2026-10-31", source="request", date=EARLIER)


def test_planner_sees_the_last_two_reports_and_what_they_carry() -> None:
    seen: list[ModelRequest] = []
    model = ScriptedModel({"plan": [ModelResponse(structured=six_task_plan())]})
    model.on_request = seen.append
    worker_dependencies = make_deps(model)
    for text, constraints in [("R1 claim.", []), ("R2 claim.", []), ("R3 claim.", [TEST_ACCOUNTS])]:
        seed_report(worker_dependencies, chat_id="c1", user_id="u1", claims=[claim("k1", text)], constraints=constraints)
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    planned = create_research_plan(load_initial_request_state(worker_dependencies, "c1", req.message_id), 0, worker_dependencies)
    turn = seen[0].messages[0].content
    assert "R2 claim." in turn and "R3 claim." in turn and "R1 claim." not in turn
    assert ("constraints in force from earlier requests:\n"
            "- Exclude account ACC-TEST-9.  (source: request, 2026-11-01)") in turn
    assert planned["carried"] == {TEST_ACCOUNTS.text: {"source": "request", "date": EARLIER}}
    assert planned["dropped"] == []


def test_constraints_carry_into_the_next_report_with_their_origin(tmp_path) -> None:
    plan = six_task_plan()
    plan["droppedConstraints"] = [{"text": OLD_PERIOD.text, "reason": "This request names its own period."}]
    seen: list[ModelRequest] = []
    model = ScriptedModel({"plan": [ModelResponse(structured=clarification("Which accounts are yours?")),
                                    ModelResponse(structured=plan)],
                           "execute": [ModelResponse(structured=t1_result())] * 3})
    model.on_request = seen.append
    worker_dependencies = make_deps(model, tasks_in_flight=1)
    seed_report(worker_dependencies, chat_id="c1", user_id="u1", claims=[claim("k1", "R claim.")],
                constraints=[TEST_ACCOUNTS, OLD_PERIOD])
    req = seed_chat_with_dataset(worker_dependencies, chat_id="c1", user_id="u1", dataset_id="a2")
    db = tmp_path / "ckpt.sqlite"
    RequestRunner(worker_dependencies, sqlite_saver(db)).start("c1", req.message_id)
    pending = worker_dependencies.repositories.messages.pending_for_request("c1", req.message_id)[0]
    RequestRunner(worker_dependencies, sqlite_saver(db)).resume("c1", req.message_id, reply_to(req, pending.message_id))
    report = next(m for m in worker_dependencies.repositories.messages.list_by_chat("c1")
                  if m.kind == "report" and m.request_message_id == req.message_id)
    by_text = {c.text: c for c in report.report.constraints}
    assert (by_text[TEST_ACCOUNTS.text].source, by_text[TEST_ACCOUNTS.text].date) == ("request", EARLIER)
    answer = by_text["User answered 'Which accounts are yours?': ACC-SAV-1 and ACC-CUR-2"]
    planned_rule = by_text[six_task_plan()["constraints"][0]]
    assert answer.source == "m3" and answer.date == planned_rule.date != EARLIER
    assert OLD_PERIOD.text not in by_text
    assert report.report.coverage["droppedConstraints"] == [
        {"text": OLD_PERIOD.text, "reason": "This request names its own period."}]
    assert all(TEST_ACCOUNTS.text in r.messages[0].content for r in seen if r.stage == "execute")


def test_carry_is_capped_and_no_constraint_vanishes_silently() -> None:
    rules = [ReportConstraint(text=f"Rule {i:02d}.", source="request", date=EARLIER) for i in range(25)]
    secret = ReportConstraint(text="password=hunter2", source="m9", date=EARLIER)
    _, newest = seed_report(make_deps(ScriptedModel({})), chat_id="c1", user_id="u1", claims=[],
                            constraints=[*rules, secret])
    carried, dropped = carried_constraints([newest], cap=20)
    assert [c.text for c in carried] == [f"Rule {i:02d}." for i in range(20)]
    assert dropped == [
        *({"text": f"Rule {i:02d}.", "reason": "more than 20 constraints carried"} for i in range(20, 25)),
        {"text": "password=hunter2", "reason": "looks like a credential"},
    ]
    in_force, by_planner = apply_drops(carried, [
        ConstraintDrop(text="rule 00", reason="A paraphrase is not a drop."),
        ConstraintDrop(text="Never carried.", reason="Unknown text is ignored."),
        ConstraintDrop(text="Rule 01.", reason="The request includes those transactions."),
    ])
    assert [c.text for c in in_force] == ["Rule 00.", *(f"Rule {i:02d}." for i in range(2, 20))]
    assert by_planner == [{"text": "Rule 01.", "reason": "The request includes those transactions."}]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_constraints_across_requests.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.worker.stages.prior`.

- [ ] **Step 3: Implement; write `prompts/plan.2.md`**

`plan.2.md` is the current plan prompt with these three lines inserted after "Do not plan a task whose answer is already in the constraints." (use the next free version if phases 2 or 3 already added `plan.2`):

```text
Constraints listed in prior_context as in force from earlier requests still apply. Do not
repeat them in your constraints. If this request contradicts one, put its exact text in
droppedConstraints with the reason.
```

`prompts.py` maps the plan stage to the new file; `plan.1.md` stays for history.

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: every test passes, including `tests/test_models.py` with `plan.2`; clean.

- [ ] **Step 5: Commit**

```bash
git add prompts/plan.2.md src/contextforge tests
git commit -m "feat: constraints carry across requests with provenance; planner prior context is the last two reports"
```

---

### Task 6: The promotion gate

**Files:**
- Create: `src/contextforge/core/memory/gate.py`, `src/contextforge/worker/stages/promote.py`
- Modify: `src/contextforge/core/ids.py` (`plus_days`), `src/contextforge/worker/stages/report.py` (promote after a COMPLETED transition), `tests/helpers.py` (`make_deps(..., auto_promote=None)`)
- Test: `tests/test_promotion_gate.py`

**Interfaces:**
- Consumes: Task 2 `AgentMemoryRecord`, `AgentMemoryRepository`, `StaleRevisionError`, `MemoryPolicy`; Task 1 `contains_credential`; `Claim`, `ReportMessage`, `RequestMessage`, `derived_id`, `now_iso`; Task 5 `seed_report`, `claim`; Task 3 `build_memory_snapshot` (test only).
- The gate's three questions, as code: **what** may be promoted is `claim_refusal` and `episode_refusal` below; **by whom** is only `promote_completed_request`, called only by the report stage (no tool, stage or endpoint writes a record any other way); **after what confirmation** is the cite stage's CONFIRMED status (two independent sources, no contradiction, spec 3.3) on a COMPLETED request, with a later request's confirmation refreshing the record rather than adding one.
- Produces:
  - `ids.plus_days(timestamp: str, days: int) -> str`, in `now_iso`'s format.
  - `gate.PromotionDecision` (frozen dataclass): `kind: MemoryKind`, `claim_id: str | None` (None for the episode), `promoted: bool`, `reason: str`, `memory_id: str | None = None`.
  - `gate.memory_id_for(user_id: str, kind: MemoryKind, content: str) -> str` = `derived_id(f"{user_id}:{kind}:{' '.join(content.lower().split())}")`, so one fact confirmed twice maps to one record.
  - `gate.claim_refusal(claim: Claim, report: ReportMessage, request: RequestMessage, policy: MemoryPolicy) -> str | None`: the first failing rule, in this order: `"owner mismatch"` (`report.user_id != request.user_id`); `"request is not COMPLETED"`; `f"claim is {claim.status}, not CONFIRMED"`; `"claim has no evidence"`; `"claim is named in the report's uncertainties"` (an uncertainty line contains the claim id, or the claim text ignoring case); `"claim looks like a credential"`; `f"claim is longer than {policy.record_max_chars} characters"`.
  - `gate.episode_refusal(report: ReportMessage, request: RequestMessage) -> str | None`: `"owner mismatch"`, `"request is not COMPLETED"`, `"report has no claims"`, `"report has uncertainties"`, `"not every claim is CONFIRMED"`, in that order.
  - `gate.episode_content(request_text: str, claims: Sequence[Claim], max_chars: int) -> str`: `f"Asked: {request} Verified: {'; '.join(claim texts)}"` with runs of whitespace collapsed to one space; when longer than `max_chars`, cut to `max_chars - 1` characters, right-stripped, plus `…`.
  - `gate.promote_report(memories: AgentMemoryRepository, report: ReportMessage, request: RequestMessage, *, now: str, policy: MemoryPolicy) -> list[PromotionDecision]`: one decision per claim in claim order, then one for the episode. A passing candidate is written by these rules: no stored record, `put(expected_revision=None)`, reason `"promoted"`; a stored `active` record, refreshed (`last_verified_at = now`, `expires_at = plus_days(now, ttl)`, `source_refs` = the stored refs then the new ones not already present, `updated_at = now`) with `expected_revision=stored.revision`, reason `"refreshed"`; any other stored status, not written, reason `f"record is {status}"`; on `StaleRevisionError`, re-read and apply the same rules once more, then reason `"concurrent update"`. A new record has `status=active`, `created_at = updated_at = last_verified_at = now`, `expires_at = plus_days(now, policy.ttl_days(kind))`, and `source_refs` = the claim's refs (for the episode, every claim's refs, without repeats, in order). It never raises for a refusal or a lost race. The module docstring names the reader: `build_memory_snapshot` (Task 3), the memory section of the user's later requests.
  - `promote.promote_completed_request(brief: RequestState, deps: WorkerDependencies) -> list[PromotionDecision]`: `[]` when `deps.memory_policy.auto_promote` is false; otherwise loads the request and its report (`derived_id(f"{request_message_id}:report")`, `LookupError` if missing), calls `promote_report` with `now_iso()` and logs each decision. `write_report` calls it only after the request moved to COMPLETED; it runs inside the report task, so a re-run repeats it and the content-derived ids keep it to one record per fact.
  - `tests/helpers.make_deps(..., auto_promote: bool | None = None)`: when given, `memory_policy = load_memory_policy().model_copy(update={"auto_promote": auto_promote})`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_promotion_gate.py
from contextforge.core.entities.agent_memory import MemoryKind
from contextforge.core.entities.message import RequestStatus
from contextforge.core.memory.gate import promote_report
from contextforge.core.memory.policy import load_memory_policy
from contextforge.core.memory.section import build_memory_snapshot
from contextforge.core.models.scripted import ScriptedModel
from contextforge.core.repositories.base import StaleRevisionError
from contextforge.core.repositories.memory import InMemoryAgentMemory
from contextforge.core.values import EvidenceRef, Locator
from contextforge.worker.stages.promote import promote_completed_request
from tests.helpers import claim, create_initial_request_state, make_deps, seed_report

NOW = "2026-11-07T10:00:00Z"
LATER = "2026-12-01T10:00:00Z"
POLICY = load_memory_policy()
ROW_12 = EvidenceRef(artifact_id="a2", locator=Locator(kind="row", value="12,13"))
PAGE_2 = EvidenceRef(artifact_id="a4", locator=Locator(kind="page", value="2"))
OWN = "ACC-SAV-1 and ACC-CUR-2 are the user's own accounts."


def test_only_verified_claims_without_uncertainty_are_promoted() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    request, report = seed_report(worker_dependencies, chat_id="c1", user_id="u1", claims=[
        claim("k1", OWN, refs=[ROW_12]),
        claim("k2", "ICICI bill unpaid.", status="UNKNOWN"),
        claim("k3", "HDFC bill paid.", refs=[]),
        claim("k4", "Decathlon refund outstanding."),
        claim("k5", "Card token=ghp_" + "a" * 36),
        claim("k6", "x" * 301),
    ], uncertainties=["Claim k4 relies on an email that may be superseded."])
    decisions = promote_report(worker_dependencies.repositories.memories, report, request, now=NOW, policy=POLICY)
    assert {d.claim_id: d.reason for d in decisions} == {
        "k1": "promoted", "k2": "claim is UNKNOWN, not CONFIRMED", "k3": "claim has no evidence",
        "k4": "claim is named in the report's uncertainties", "k5": "claim looks like a credential",
        "k6": "claim is longer than 300 characters", None: "report has uncertainties"}
    [record] = worker_dependencies.repositories.memories.list_by_user("u1")
    assert (record.kind, record.status, record.revision, record.content) == ("semantic", "active", 1, OWN)
    assert record.source_refs == [ROW_12] and record.expires_at == "2027-02-05T10:00:00Z"
    running, unfinished = seed_report(worker_dependencies, chat_id="c1", user_id="u1", claims=[claim("k1", "Another fact.")],
                                      request_status=RequestStatus.RUNNING)
    later = promote_report(worker_dependencies.repositories.memories, unfinished, running, now=NOW, policy=POLICY)
    assert {d.reason for d in later} == {"request is not COMPLETED"}


def test_a_fact_confirmed_again_refreshes_one_record_and_a_replay_adds_none() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    first_request, first = seed_report(worker_dependencies, chat_id="c1", user_id="u1", claims=[claim("k1", OWN, refs=[ROW_12])])
    promote_report(worker_dependencies.repositories.memories, first, first_request, now=NOW, policy=POLICY)
    promote_report(worker_dependencies.repositories.memories, first, first_request, now=NOW, policy=POLICY)  # report task re-ran
    second_request, second = seed_report(worker_dependencies, chat_id="c2", user_id="u1",
                                         claims=[claim("k9", f"  {OWN.upper()} ", refs=[PAGE_2])])
    decisions = promote_report(worker_dependencies.repositories.memories, second, second_request, now=LATER, policy=POLICY)
    [fact] = [r for r in worker_dependencies.repositories.memories.list_by_user("u1") if r.kind == MemoryKind.SEMANTIC]
    assert decisions[0].reason == "refreshed" and fact.content == OWN
    assert fact.source_refs == [ROW_12, PAGE_2] and fact.last_verified_at == LATER
    assert fact.expires_at == "2027-03-01T10:00:00Z"


def test_a_clean_report_becomes_an_episode_read_as_history() -> None:
    worker_dependencies = make_deps(ScriptedModel({}))
    request, report = seed_report(worker_dependencies, chat_id="c1", user_id="u1",
                                  claims=[claim("k1", OWN), claim("k2", "Netflix rose from 649 to 799.")])
    decisions = promote_report(worker_dependencies.repositories.memories, report, request, now=NOW, policy=POLICY)
    assert [d.reason for d in decisions] == ["promoted", "promoted", "promoted"]
    records = worker_dependencies.repositories.memories.list_by_user("u1")
    episode = next(r for r in records if r.kind == MemoryKind.EPISODIC)
    assert episode.content.startswith("Asked: Earlier request. Verified: ACC-SAV-1") and len(episode.content) <= 300
    assert episode.source_refs == [ROW_12] and episode.expires_at == "2027-11-07T10:00:00Z"
    text = build_memory_snapshot("AGENT", user_id="u1", user=None, records=records, now=NOW, policy=POLICY)["text"]
    assert text.index("<past_episodes>") > text.index("<learned_facts>") and "Asked: Earlier request." in text


def test_a_lost_race_is_retried_once_and_never_raises() -> None:
    class Racing(InMemoryAgentMemory):
        def __init__(self, losses: int) -> None:
            super().__init__()
            self.losses = losses

        def put(self, record, *, expected_revision):
            if self.losses:
                self.losses -= 1
                raise StaleRevisionError(expected_revision, 7)
            return super().put(record, expected_revision=expected_revision)

    worker_dependencies = make_deps(ScriptedModel({}))
    request, report = seed_report(worker_dependencies, chat_id="c1", user_id="u1", claims=[claim("k1", OWN)],
                                  uncertainties=["Unrelated."])
    assert promote_report(Racing(1), report, request, now=NOW, policy=POLICY)[0].reason == "promoted"
    assert promote_report(Racing(2), report, request, now=NOW, policy=POLICY)[0].reason == "concurrent update"


def test_the_report_stage_promotes_only_when_enabled() -> None:
    for enabled, expected in [(False, 0), (True, 2)]:  # 2 = the fact and the episode
        worker_dependencies = make_deps(ScriptedModel({}), auto_promote=enabled)
        request, _ = seed_report(worker_dependencies, chat_id="c1", user_id="u1", claims=[claim("k1", OWN)])
        promote_completed_request(create_initial_request_state(request), worker_dependencies)
        assert len(worker_dependencies.repositories.memories.list_by_user("u1")) == expected
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_promotion_gate.py -v`
Expected: FAIL, `ModuleNotFoundError: contextforge.core.memory.gate`.

- [ ] **Step 3: Implement `plus_days`, `gate.py`, `promote.py`, and the call in `write_report`**

- [ ] **Step 4: Run the whole suite**

Run: `uv run pytest -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: every test passes; clean.

- [ ] **Step 5: Commit**

```bash
git add src/contextforge tests
git commit -m "feat: promotion gate writes verified facts and episodes with provenance and expiry"
```

---

### Task 7: golden-001 with the standing instruction from memory, and phase close

**Files:**
- Create: `evals/cases/golden-001-instructions.md`
- Modify: `evals/cases/golden-001.md` (the phase 7 row names the variant), phase 2's eval harness (find its case loading with `grep -rn "golden-001" src evals tests`) and its test module, `docs/progress.md`, `README.md`
- Test: phase 2's harness test module

**Interfaces:**
- Consumes: Task 1 `patch_me`, `MePatch`; phase 2's harness (how it builds `WorkerDependencies`, seeds a case and scores it).
- Produces: a case variant that seeds `User.instructions` through `patch_me` before the request is submitted, and leaves the request text without the instruction.

- [ ] **Step 1: Write the case file**

```markdown
# golden-001-instructions: the standing instruction from memory

Recorded 7 November 2026 for phase 7. Identical to golden-001 (request, fixtures, expected plan, checks, trace assertions) except where the standing instruction lives.

| | golden-001 before phase 7 | golden-001-instructions |
|---|---|---|
| Request text | as the phase 2 harness supplies it, with the standing instruction | the golden-001 request only |
| `User.instructions` | empty | "Transfers between my own accounts are never spending. A card purchase and its bill payment count once." |

The harness seeds the instruction with `patch_me` before submitting the request.

What it proves: the instruction reaches every call through the memory section, not the request.

Checks, in addition to golden-001's:
- Every ModelCall of the request has a `contextVersion` starting `mem:1+`, and all share one memory version.
- The 4 own-account transfer pairs are absent from every total and the 2 card settlements are counted once, exactly as golden-001 requires.
- The score is not lower than golden-001's on the same day and model.
```

In `golden-001.md`, the phase 7 row becomes: "the standing instruction comes from `User.instructions` instead of the prompt (`golden-001-instructions`)".

- [ ] **Step 2: Extend the harness and its test**

A case may name `userInstructions`; before submitting, the harness calls `patch_me(<its repos>.users, user_id=<case user>, email=<case user email>, patch=MePatch(instructions=...))` and submits the request text unchanged. Add one assertion to the harness's case-loading test: after seeding `golden-001-instructions`, the user's instructions equal the golden instruction and the request text does not contain it.

Run: `uv run pytest -v && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: every test passes; clean.

- [ ] **Step 3: Run the live evals (needs the Task 7 prerequisites)**

With phase 2's harness command (as its README documents), run `golden-001` and `golden-001-instructions`. Expected: the variant's deterministic checks equal golden-001's; its trace shows one `mem:1+…` version.

- [ ] **Step 4: Compare automatic promotion against the no-episode baseline**

Set `autoPromote: true` in `config/memory.yaml` locally (do not commit it). Run `golden-001` twice in a row for the same user: the first run promotes, the second reads what was promoted. Record in `docs/progress.md`: the step 3 score (the baseline), the second run's score, and the records the first run promoted with their reasons. Revert the local change.

- [ ] **Step 5: Update `docs/progress.md` and `README.md`**

Progress: the phase 7 row becomes `Done <date>`; the log entry lists what was verified (expectations 6 and 22, E14, byte stability, constraint carry, the gate), the two comparisons, and the deviations below. README: one paragraph on `GET`/`PATCH /me` and `config/memory.yaml`.

- [ ] **Step 6: Commit**

```bash
git add evals docs/progress.md README.md src tests
git commit -m "docs: phase 7 complete; golden-001 instruction variant and promotion baseline"
```

---

## Deviations from the spec, decided in this plan

- Memory is read once per request, in the `load_brief` task, and kept in the brief (`RequestState.memory`). The spec allows sections 1 and 2 to change with `contextVersion`; this plan is stricter, so a plan made under one set of instructions is never executed under another. An edit applies from the next request.
- The memory version hashes everything appended to `agent-memory.md` (instructions and the selected learned records), not the instructions alone, so a change in either invalidates the cached prefix.
- Learned records render in the memory block with their verification date and evidence. These are record content, covered by the version hash, not a call timestamp.
- Standing instructions are not copied into `report.constraints` and are not carried: they reach every call through the memory section and are re-read for each request, so a deleted instruction cannot outlive its edit. The spec's "standing instruction" constraint source is therefore unused.
- A clarification-answer constraint records the answer's (clarification_response) message id as its source, since that message holds the user's words.
- Only the newest prior report's constraints are carried (it re-records what it carried); the older of the two reports contributes claims only. Carried constraints are capped at 20 (`config/memory.yaml`); the overflow and any credential-shaped constraint are listed, with reasons, in the new report's `coverage.droppedConstraints`, as are the planner's drops. The spec names no field for drops.
- Prior context no longer includes non-report messages (spec 8.2 row 8).
- `AgentMemoryRecord` omits `tags` and `artifactRef` (nothing reads them yet); `lastVerifiedAt` and `expiresAt` are required, because the only writer is the verification gate.
- Records are selected by recency under a count and character cap, not by keyword or embedding match (spec 8.7), until a user has more records than the cap.
- Automatic promotion ships behind `autoPromote: false` until the baseline comparison in Task 7. Explicit "remember this" record writes, record deletion and supersession are not built; explicit memory in phase 7 is E14 (instructions and preferences), and retrieval already excludes `superseded` and `deleted`.
- Memory limits live in a new `config/memory.yaml`, not `config/budgets.yaml`, so this phase does not edit the file phases 2 and 3 extend.
- `PlannerOutput` gains `droppedConstraints`; the plan prompt becomes `plan.2`.
- `GET /me` creates the User on first sight; refreshing the email from the identity token on login waits for real identity (phase 10).
- Expectation 8 (`Chat.lastRequestStatus`, assigned to phase 7 by system-design 14 and progress decision 6) is outside this plan's brief and not built here.

## Prerequisites (outside the code)

| Before | What the user does |
|---|---|
| Task 1 | Merge phases 3 and 4 to main and branch phase 7 from it. Phase 3 supplies the cite call and `Claim.status` CONFIRMED; phase 4 supplies the E14 route, the 400 `validation` error mapping and the signed-in test client. |
| Task 4 | Cancel any request left `WAITING_USER` or `RUNNING` in a local SQLite checkpoint from earlier phases: its checkpointed brief has no memory snapshot and would fail on resume. |
| Task 7 | Merge phase 2 (eval harness, Bedrock adapter). Keep the `aws configure` profile with Amazon Nova access in ap-southeast-2 and `LANGSMITH_API_KEY` in `.env` that phase 2 set up. Allow about $1.60 of model usage for four golden-001 runs (the case caps each at $0.40). No new IAM permissions or keys. |
| After Task 7 | Decide whether to set `autoPromote: true`, from the comparison recorded in `docs/progress.md`. |
| If phase 10 has merged | Its DynamoDB `Repositories` factory needs `users` and `memories` adapters (item shapes on the Task 1 and Task 2 Protocols) before AWS mode serves E14 or runs a request. This plan does not write them. |
