"""Context Builder (spec 8.2): one model call's context, assembled in eight fixed sections.

A pure function: stages fetch the records, the builder renders and measures them. It reads no
clock and invents no ids, so the same inputs give byte-identical output and the stable prefix
(the two system blocks) stays cacheable across calls.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AnyMessage

from contextforge.core.context.budgets import Budgets
from contextforge.core.context.tokens import estimate_tokens
from contextforge.core.entities.message import Message, ReportMessage, RequestMessage
from contextforge.core.models.base import ToolSpec
from contextforge.core.state import Fact, OpenQuestion


@dataclass(frozen=True)
class SourceLine:
    """One Artifact in the ``<sources>`` block; ``detail`` is free text such as row counts."""

    artifact_id: str
    kind: str
    name: str
    detail: str


@dataclass(frozen=True)
class ContextInputs:
    """Everything one call may see, already fetched; each stage fills the sections it uses."""

    memory: str
    stage_prompt: str
    chat_id: str
    request: RequestMessage | None
    preferences: dict[str, str]
    constraints: list[str]
    # Oldest first; only the newest ``Budgets.facts_cap`` are rendered.
    facts: list[Fact]
    open_questions: list[OpenQuestion]
    next_action: str
    dependency_summaries: list[str]
    evidence_slices: list[str]
    tools: list[ToolSpec]
    sources: list[SourceLine]
    prior_messages: list[Message]
    loop_messages: list[AnyMessage]
    # The planner's code-written ``<context>`` block (spec 9.1): today, the resolved period and
    # source readiness. Rendered right after the request; empty for every other stage.
    context_block: str = ""


@dataclass(frozen=True)
class BuiltContext:
    """``system`` and ``user_text`` hold sections 1 to 6; tools travel as specs, section 8 as
    ``messages``. ``sections`` pairs each of the eight section names with its token estimate.
    """

    system: list[str]
    user_text: str
    messages: list[AnyMessage]
    sections: list[tuple[str, int]]
    # Sections 1 to 7: what compaction cannot shrink. Section 8 gets the rest of the window.
    fixed_tokens: int


def build_context(inputs: ContextInputs, budgets: Budgets) -> BuiltContext:
    """Render and measure ``inputs``; ``loop_messages`` pass through untouched.

    Compaction is not done here: the caller compacts the messages against
    ``budgets.messages_budget(fixed_tokens)`` before building.
    """
    # Nothing is interpolated into the system blocks: a date or id there would change the
    # cached prefix on every call.
    system = [inputs.memory, inputs.stage_prompt]
    user_sections = {
        "brief": _render_brief(inputs),
        "facts": _block(
            "facts", [_fact_line(f) for f in newest_facts(inputs.facts, budgets.facts_cap)]
        ),
        "open_questions": _block(
            "open_questions", [_open_question_line(q) for q in inputs.open_questions]
        ),
        "dependencies": _block("dependencies", [f"- {s}" for s in inputs.dependency_summaries]),
        "evidence": _block("evidence", [f"- {s}" for s in inputs.evidence_slices]),
    }
    sections = [
        # Section 1 is the whole system prefix: the memory file and the stage prompt.
        ("memory", _tokens(system)),
        *((name, _tokens([text])) for name, text in user_sections.items()),
        ("tools", _tokens([_tool_text(tool) for tool in inputs.tools])),
        ("messages", messages_tokens(inputs.loop_messages)),
    ]
    return BuiltContext(
        system=system,
        user_text=_join(user_sections.values()),
        messages=list(inputs.loop_messages),
        sections=sections,
        fixed_tokens=sum(tokens for name, tokens in sections if name != "messages"),
    )


def _render_brief(inputs: ContextInputs) -> str:
    """Section 2 in the spec 9.1 tag order.

    Request, context, preferences, constraints, next action, sources, prior context.
    """
    request = inputs.request
    preferences = "   ".join(f"{key}: {value}" for key, value in sorted(inputs.preferences.items()))
    return _join(
        [
            _request_block(request),
            _block("context", inputs.context_block.splitlines()),
            f"<user_preferences>{preferences or '(none)'}</user_preferences>",
            _block("constraints", [f"- {c}" for c in inputs.constraints], empty="(none yet)"),
            f"<next_action>{inputs.next_action}</next_action>" if inputs.next_action else "",
            _block(
                "sources",
                [f"{s.artifact_id}  {s.kind}  {s.name}  {s.detail}" for s in inputs.sources],
                attrs=f' chat_id="{inputs.chat_id}"',
                empty="(none)",
            ),
            _block("prior_context", [line for m in inputs.prior_messages for line in _prior(m)]),
        ]
    )


def _request_block(request: RequestMessage | None) -> str:
    if request is None:
        return ""
    return f'<request message_id="{request.message_id}">\n{request.content}\n</request>'


def _block(tag: str, lines: list[str], *, attrs: str = "", empty: str = "") -> str:
    """``lines`` inside ``<tag>``; with none, ``empty`` inline, or nothing if ``empty`` is ""."""
    if lines:
        body = "\n".join(lines)
        return f"<{tag}{attrs}>\n{body}\n</{tag}>"
    return f"<{tag}{attrs}>{empty}</{tag}>" if empty else ""


def _join(blocks: Iterable[str]) -> str:
    return "\n".join(block for block in blocks if block)


def newest_facts(facts: list[Fact], cap: int) -> list[Fact]:
    """The last ``cap`` facts: the brief keeps and shows only the newest."""
    # Not ``facts[-cap:]``: with a cap of 0 that slice keeps every fact.
    return facts[max(0, len(facts) - cap) :]


def _fact_line(fact: Fact) -> str:
    refs = json.dumps(fact["evidence_refs"], sort_keys=True, ensure_ascii=False)
    return f"- {fact['text']}  evidence: {refs}"


def _open_question_line(question: OpenQuestion) -> str:
    resolved = question["resolved_by"]
    return f"- {question['text']}" + (f"  (resolved by {resolved})" if resolved else "")


def _prior(message: Message) -> list[str]:
    """A report contributes its claims, which outlive its transcript; other kinds their text."""
    if isinstance(message, ReportMessage):
        return ["report:", *(f"- {claim.text}" for claim in message.report.claims)]
    return [f"{message.kind.value}: {message.content}"]


def _tool_text(tool: ToolSpec) -> str:
    return json.dumps(tool.model_dump(mode="json", by_alias=True), sort_keys=True)


def messages_tokens(messages: list[AnyMessage]) -> int:
    """Estimated tokens of loop messages: the one rule the builder and compaction both use.

    Every message costs at least one token, even with empty text, because it is still sent.
    """
    return sum(estimate_tokens(_message_text(m)) for m in messages)


def _message_text(message: AnyMessage) -> str:
    """The text a loop message is measured by: its content plus, on an AI turn, its tool calls."""
    content = message.content
    text = content if isinstance(content, str) else json.dumps(content, sort_keys=True)
    if isinstance(message, AIMessage) and message.tool_calls:
        text += json.dumps(message.tool_calls, sort_keys=True)
    return text


def _tokens(texts: list[str]) -> int:
    # An absent part costs nothing; estimate_tokens alone would count it as one token.
    return sum(estimate_tokens(text) for text in texts if text)
