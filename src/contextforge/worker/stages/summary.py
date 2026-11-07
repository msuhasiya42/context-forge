"""Summary stage (F10 ruling 2): the few sentences that open every report, one per part of the
request its findings answer, then its assessment.

One call on the cheap model reads only the report's goal, claims, conclusions and the parts not
answered, never page text. Its text is kept only when every number and proper noun in it
appears in what it read; otherwise, or when the call fails, the summary is built from the
claims without a model.
"""

import logging
import re
from collections.abc import Iterable

from langchain_core.messages import HumanMessage

from contextforge.core.context.prompts import load_prompt, prompt_version
from contextforge.core.entities.message import ReportBody
from contextforge.core.entities.model_call import Stage
from contextforge.core.models.base import ModelRequest
from contextforge.core.models.errors import ModelError
from contextforge.core.state import RequestState
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.stages.cite import claim_key
from contextforge.worker.stages.execute import TaskOutcome

logger = logging.getLogger(__name__)

# About 120 words asked for, at most 1.5 tokens a word in English, with room for numbers.
SUMMARY_MAX_TOKENS = 400
# The prompt asks for at most 120 words; a text this long is no summary.
MAX_SUMMARY_WORDS = 160

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_WORD = re.compile(r"[^\W\d_]+")
# Words that start a sentence without naming anything. Any other capitalised word, wherever it
# stands, must be a word of the report.
_SENTENCE_STARTERS = frozenset(
    "a an the this that these those it its they their there here both each either neither "
    "all any most many some several few no not none in on at by for from of over under during "
    "between across with without within to into after before since until as if when while "
    "where although though because but and or nor so yet also however overall still then".split()
)


def summarize(
    brief: RequestState,
    outcomes: list[TaskOutcome],
    body: ReportBody,
    not_answered: list[str],
    deps: WorkerDeps,
) -> str:
    """The summary model's text when it names only what the report holds, else the summary
    built from the claims (``fallback_summary``). ``body`` has at least one claim."""
    written = _written_summary(brief, body, not_answered, deps)
    if written is not None:
        sources = [
            brief["goal"],
            *(claim.text for claim in body.claims),
            *(conclusion.text for conclusion in body.conclusions),
            *not_answered,
        ]
        unsupported = unsupported_terms(written, sources)
        if not unsupported and len(written.split()) <= MAX_SUMMARY_WORDS:
            return written
        logger.warning(
            "summary not used: %d words, not in the report: %s", len(written.split()), unsupported
        )
    return fallback_summary(outcomes, body)


def unsupported_terms(summary: str, sources: Iterable[str]) -> list[str]:
    """The numbers and proper nouns of ``summary`` that no text of ``sources`` holds, in order;
    empty when it names only what they name.

    A number matches with its thousands separators left out ("1,545" is "1545"). A proper noun
    is a capitalised word, compared ignoring case; one starting a sentence is a common word,
    not a name, when it is in ``_SENTENCE_STARTERS``.
    """
    text = "\n".join(sources)
    numbers = {_number(found) for found in _NUMBER.findall(text)}
    words = {word.casefold() for word in _WORD.findall(text)}
    missing = [found for found in _NUMBER.findall(summary) if _number(found) not in numbers]
    missing += [
        match.group()
        for match in _WORD.finditer(summary)
        if _is_name(summary, match) and match.group().casefold() not in words
    ]
    return missing


def fallback_summary(outcomes: list[TaskOutcome], body: ReportBody) -> str:
    """The first claim of each task, in plan order (task ids sort in it), then the first
    conclusion, cut at ``MAX_SUMMARY_WORDS``: built without a model from what the report holds."""
    claims = {claim_key(claim.text): claim.text for claim in body.claims}
    firsts: list[str] = []
    for outcome in sorted(outcomes, key=lambda outcome: outcome["task_id"]):
        texts = (claims.get(claim_key(fact["text"])) for fact in outcome["facts"])
        first = next((text for text in texts if text and text not in firsts), None)
        if first is not None:
            firsts.append(first)
    lines = [*(firsts or [body.claims[0].text]), *(c.text for c in body.conclusions[:1])]
    words = " ".join(_sentence(line) for line in lines).split()
    if len(words) <= MAX_SUMMARY_WORDS:
        return " ".join(words)
    return " ".join(words[:MAX_SUMMARY_WORDS]) + "…"


def _written_summary(
    brief: RequestState, body: ReportBody, not_answered: list[str], deps: WorkerDeps
) -> str | None:
    """The summary model's text on one line; None when the call fails or returns no text. The
    call is recorded as a ``summary`` ModelCall of the request and counts against its budget."""
    model = deps.model_for(brief["request_message_id"], memory_version=brief["memory"]["version"])
    request = ModelRequest(
        stage=Stage.SUMMARY,
        # Routed like compress: the cheap stage (config/models.yaml).
        model=deps.model_id(Stage.SUMMARY),
        system=[brief["memory"]["text"], load_prompt(prompt_version(Stage.SUMMARY))],
        messages=[HumanMessage(_summary_input(brief["goal"], body, not_answered))],
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    try:
        response = model.invoke(request)
    except ModelError as exc:
        logger.warning("summary call failed: %s", exc)
        return None
    return " ".join((response.text or "").split()) or None


def _summary_input(goal: str, body: ReportBody, not_answered: list[str]) -> str:
    """The request's goal and the report's findings, conclusions and gaps, each block's lines
    made safe to sit inside it: claims may quote pages."""
    findings = [f"{n}. {_safe(claim.text)}" for n, claim in enumerate(body.claims, 1)]
    conclusions = [f"- {_safe(c.text)} ({c.confidence} confidence)" for c in body.conclusions]
    gaps = [f"- {_safe(line)}" for line in not_answered]
    blocks = [
        f"Request: {_safe(goal)}",
        _block("findings", findings),
        _block("conclusions", conclusions),
        _block("not_answered", gaps),
    ]
    return "\n".join(blocks)


def _block(tag: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"<{tag}>\n{body}\n</{tag}>"


def _safe(text: str) -> str:
    """``text`` on one line, unable to close a block or forge a tag."""
    return " ".join(text.split()).replace("<", "&lt;").replace(">", "&gt;")


def _number(text: str) -> str:
    return text.replace(",", "")


def _is_name(text: str, match: re.Match[str]) -> bool:
    """Whether the word ``match`` found in ``text`` may name something: it is capitalised, and
    it is not a common word starting a sentence."""
    word = match.group()
    if not word[0].isupper():
        return False
    before = text[: match.start()].rstrip(" \t\"'“‘(")
    starts_sentence = not before or before[-1] in ".!?:;\n"
    return not (starts_sentence and word.casefold() in _SENTENCE_STARTERS)


def _sentence(text: str) -> str:
    """``text`` on one line, ending as a sentence."""
    line = " ".join(text.split())
    return line if line.endswith((".", "!", "?")) else f"{line}."
