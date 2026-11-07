# tests/test_context.py
import dataclasses
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from contextforge.core.context.budgets import BUDGETS_PATH, Budgets, load_budgets
from contextforge.core.context.builder import ContextInputs, SourceLine, build_context
from contextforge.core.context.tool_results import process_rows, raw_result_id
from contextforge.core.entities.message import (
    Claim,
    MessageKind,
    ReportBody,
    ReportMessage,
    RequestMessage,
    RequestStatus,
    Sender,
    TextMessage,
)
from contextforge.core.models.base import ToolSpec
from contextforge.core.repositories.memory import Repositories
from contextforge.core.storage import MemoryBlobStore
from contextforge.core.values import EvidenceRef, Locator, LocatorKind


def _inputs() -> ContextInputs:
    request = RequestMessage(
        message_id="m1",
        chat_id="c1",
        user_id="u1",
        sender=Sender.USER,
        kind=MessageKind.REQUEST,
        content="Compare October spending with September.",
        status=RequestStatus.RUNNING,
        created_at="2026-11-07T10:00:00Z",
        updated_at="2026-11-07T10:00:00Z",
    )
    ref = {"artifactId": "a2", "locator": {"kind": "row", "value": "12"}}
    return ContextInputs(
        memory="MEM",
        stage_prompt="EXECUTE",
        request=request,
        # Deliberately out of key order: rendering must sort them.
        preferences={"timezone": "Asia/Kolkata", "currency": "INR"},
        constraints=["Exclude transfers between own accounts.", "Report amounts in INR."],
        facts=[
            {"text": "Groceries rose 12%.", "evidence_refs": [ref]},
            {"text": "Netflix charged twice.", "evidence_refs": [ref, ref]},
            {"text": "Rent unchanged.", "evidence_refs": [ref]},
        ],
        open_questions=[{"text": "Which account_ids are the user's?", "resolved_by": None}],
        next_action="Run t1.",
        dependency_summaries=["t1: 3 transfer pairs found."],
        evidence_slices=["a2 rows 10-14: ..."],
        tools=[
            ToolSpec(
                name="sql_query",
                description="Run read-only SQL over a dataset.",
                input_schema={"type": "object", "properties": {"sql": {"type": "string"}}},
            )
        ],
        sources=[SourceLine("a2", "dataset", "transactions_sep_oct.parquet", "rows: 1,240")],
        prior_messages=[],
        loop_messages=[
            HumanMessage("Identify transfers between accounts."),
            AIMessage(
                "",
                tool_calls=[{"name": "sql_query", "args": {"sql": "select 1"}, "id": "x1"}],
            ),
        ],
    )


def test_budgets_messages_budget() -> None:
    b: Budgets = load_budgets()
    assert b.messages_budget(fixed_tokens=2500) == 40000 - 4000 - 4000 - 2500


def test_builder_sections_in_spec_order_and_system_blocks() -> None:
    inputs = ContextInputs(
        memory="MEM",
        stage_prompt="PLAN",
        request=None,
        preferences={"currency": "INR"},
        constraints=["c1"],
        facts=[],
        open_questions=[],
        next_action="n",
        dependency_summaries=[],
        evidence_slices=[],
        tools=[],
        sources=[SourceLine("a2", "dataset", "t.parquet", "rows: 60")],
        prior_messages=[],
        loop_messages=[],
    )
    built = build_context(inputs, load_budgets())
    assert built.system == ["MEM", "PLAN"]
    assert [name for name, _ in built.sections] == [
        "memory",
        "brief",
        "facts",
        "open_questions",
        "dependencies",
        "evidence",
        "tools",
        "messages",
    ]
    assert "<sources chat_id=" in built.user_text and "a2  dataset" in built.user_text


def test_facts_are_capped_to_newest() -> None:
    facts = [{"text": f"f{i}", "evidence_refs": []} for i in range(35)]
    inputs = ContextInputs(
        memory="M",
        stage_prompt="P",
        request=None,
        preferences={},
        constraints=[],
        facts=facts,
        open_questions=[],
        next_action="n",
        dependency_summaries=[],
        evidence_slices=[],
        tools=[],
        sources=[],
        prior_messages=[],
        loop_messages=[],
    )
    text = build_context(inputs, load_budgets()).user_text
    assert "f34" in text and "f4" not in text


def test_process_rows_boundary_at_cap() -> None:
    repos, blobs = Repositories.in_memory(), MemoryBlobStore()
    fifty = [{"i": i} for i in range(50)]
    r = process_rows(
        fifty,
        cap=50,
        artifacts=repos.artifacts,
        blobs=blobs,
        chat_id="c1",
        user_id="u1",
        name="q",
        artifact_id="a6",
    )
    assert r.artifact_id is None and len(r.rows) == 50 and r.text == "showing 50 of 50 rows"
    r2 = process_rows(
        fifty + [{"i": 50}],
        cap=50,
        artifacts=repos.artifacts,
        blobs=blobs,
        chat_id="c1",
        user_id="u1",
        name="q",
        artifact_id="a7",
    )
    r3 = process_rows(
        fifty + [{"i": 50}],
        cap=50,
        artifacts=repos.artifacts,
        blobs=blobs,
        chat_id="c1",
        user_id="u1",
        name="q",
        artifact_id="a7",
    )  # replay: same id, one artifact
    assert r3.artifact_id == "a7" and len(repos.artifacts.list_by_chat("c1")) == 1
    assert (
        r2.artifact_id
        and len(r2.rows) == 50
        and r2.text == f"showing 50 of 51 rows; 1 more row(s) in artifact {r2.artifact_id}"
    )
    art = repos.artifacts.get("c1", r2.artifact_id)
    assert art and art.kind == "raw_result" and art.status == "AVAILABLE"
    assert blobs.get(art.storage_ref).count(b"\n") == 51


def test_process_rows_1240_rows_keeps_50() -> None:
    repos, blobs = Repositories.in_memory(), MemoryBlobStore()
    r = process_rows(
        [{"i": i} for i in range(1240)],
        cap=50,
        artifacts=repos.artifacts,
        blobs=blobs,
        chat_id="c1",
        user_id="u1",
        name="q",
        artifact_id="a8",
    )
    assert len(r.rows) == 50 and r.total_rows == 1240 and "1190 more row(s)" in r.text


def test_zero_rows_is_a_valid_result() -> None:
    repos, blobs = Repositories.in_memory(), MemoryBlobStore()
    r = process_rows(
        [],
        cap=50,
        artifacts=repos.artifacts,
        blobs=blobs,
        chat_id="c1",
        user_id="u1",
        name="q",
        artifact_id="a8",
    )
    assert r.rows == [] and r.artifact_id is None and r.text == "showing 0 of 0 rows"


def test_wide_cells_are_shaped_once() -> None:
    repos, blobs = Repositories.in_memory(), MemoryBlobStore()
    r = process_rows(
        [{"c": "x" * 500}],
        cap=50,
        artifacts=repos.artifacts,
        blobs=blobs,
        chat_id="c1",
        user_id="u1",
        name="q",
        artifact_id="a9",
    )
    assert r.rows[0]["c"].endswith("…[+300 chars]") and len(r.rows[0]["c"]) == 200 + len(
        "…[+300 chars]"
    )


def test_build_context_is_byte_stable() -> None:
    inputs = _inputs()
    assert (
        build_context(inputs, load_budgets()).user_text
        == build_context(inputs, load_budgets()).user_text
    )
    assert (
        build_context(inputs, load_budgets()).system == build_context(inputs, load_budgets()).system
    )


# Beyond the brief: the controller's rulings on budgets, ids, rendering and shaping.


def test_messages_budget_rounds_the_margin_exactly() -> None:
    # 40000 * 0.07 is 2800.0000000000005 in floating point; a naive ceil would give 2801.
    b = dataclasses.replace(load_budgets(), margin_fraction=0.07)
    assert b.messages_budget(fixed_tokens=0) == 40000 - 4000 - 2800


def test_load_budgets_fails_loudly_on_a_missing_key(tmp_path: Path) -> None:
    lines = BUDGETS_PATH.read_text(encoding="utf-8").splitlines()
    path = tmp_path / "budgets.yaml"
    path.write_text("\n".join(x for x in lines if not x.startswith("factsCap")), encoding="utf-8")
    with pytest.raises(KeyError, match="factsCap"):
        load_budgets(path)


def test_raw_result_id_is_deterministic_and_ulid_shaped() -> None:
    first = raw_result_id("t1", "x1")
    assert first == raw_result_id("t1", "x1") and len(first) == 26
    assert first != raw_result_id("t1", "x2")


def test_user_text_follows_the_spec_layout() -> None:
    built = build_context(_inputs(), load_budgets())
    text = built.user_text
    tags = [
        '<request message_id="m1">',
        "<user_preferences>",
        "<constraints>",
        '<sources chat_id="c1">',
        "<facts>",
        "<open_questions>",
        "<dependencies>",
        "<evidence>",
    ]
    positions = [text.index(tag) for tag in tags]
    assert positions == sorted(positions)
    assert "<user_preferences>currency: INR   timezone: Asia/Kolkata</user_preferences>" in text
    assert "a2  dataset  transactions_sep_oct.parquet  rows: 1,240" in text
    # No prior messages: the block is left out; tools go to the model as specs, not text.
    assert "<prior_context>" not in text and "sql_query" not in text
    sections = dict(built.sections)
    assert sections["tools"] > 0 and sections["messages"] > 0
    assert built.fixed_tokens == sum(
        tokens for name, tokens in built.sections if name != "messages"
    )
    assert built.messages == _inputs().loop_messages


def test_prior_context_renders_report_claims_and_other_kinds() -> None:
    common = {
        "chat_id": "c1",
        "user_id": "u1",
        "request_message_id": "m0",
        "created_at": "2026-11-01T10:00:00Z",
        "updated_at": "2026-11-01T10:00:00Z",
    }
    ref = EvidenceRef(artifact_id="a2", locator=Locator(kind=LocatorKind.ROW, value="3"))
    report = ReportMessage(
        message_id="m2",
        sender=Sender.ASSISTANT,
        kind=MessageKind.REPORT,
        content="Report",
        report=ReportBody(
            claims=[
                Claim(claim_id="k1", text="September spend was INR 41,000.", evidence_refs=[ref])
            ],
            inputs_consulted=[],
            uncertainties=[],
            coverage={},
        ),
        **common,
    )
    note = TextMessage(
        message_id="m3", sender=Sender.USER, kind=MessageKind.TEXT, content="Thanks", **common
    )
    inputs = dataclasses.replace(_inputs(), prior_messages=[report, note])
    text = build_context(inputs, load_budgets()).user_text
    assert "September spend was INR 41,000." in text and "text: Thanks" in text
    assert text.index("<prior_context>") > text.index("<sources")


def test_shaping_caps_columns_and_leaves_the_blob_unshaped() -> None:
    repos, blobs = Repositories.in_memory(), MemoryBlobStore()
    wide = [{f"k{j}": "y" * 250 for j in range(25)} for _ in range(3)]
    r = process_rows(
        wide,
        cap=2,
        artifacts=repos.artifacts,
        blobs=blobs,
        chat_id="c1",
        user_id="u1",
        name="q",
        artifact_id="a10",
    )
    assert list(r.rows[0]) == [f"k{j}" for j in range(20)]
    assert r.rows[0]["k0"].endswith("…[+50 chars]")
    art = repos.artifacts.get("c1", "a10")
    assert (
        art and b"y" * 250 in blobs.get(art.storage_ref) and b'"k24"' in blobs.get(art.storage_ref)
    )
