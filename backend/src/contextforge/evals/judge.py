"""The offline LLM judge: claim groundedness and report coverage/quality for finished runs.

It never runs inside a user request. Each call's answer is read leniently and checked; a schema
failure is sent back once with what was wrong, and a call that still fails becomes a
``judge_error`` verdict for the items it covered, never a dropped item.
"""

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from contextforge.core.context.prompts import load_prompt
from contextforge.core.entities import Entity
from contextforge.core.entities.model_call import Stage
from contextforge.core.llm.base import ModelClient, ModelRequest
from contextforge.core.llm.errors import ModelError
from contextforge.worker.stages.errors import one_line

logger = logging.getLogger(__name__)

CLAIMS_PROMPT = "judge_claims.1"
REPORT_PROMPT = "judge_report.1"
REASON_WORDS = 30
# Bounds one report call's input; a longer report is judged on its head and says so.
REPORT_MAX_CHARS = 60_000

ClaimLabel = Literal["supported", "partial", "unsupported", "contradicted"]
PartLabel = Literal["answered", "partial", "missing"]
JUDGE_ERROR = "judge_error"


class ClaimEvidence(Entity):
    """One report claim with what it cites: the excerpt and up to ~1,500 characters of the
    saved page snapshot around it (the snapshot's head when the excerpt is not in it)."""

    claim_id: str
    claim: str
    cited_url: str
    excerpt: str
    context: str


class ClaimVerdict(Entity):
    claim_id: str
    verdict: ClaimLabel | Literal["judge_error"]
    reason: str


class PartVerdict(Entity):
    part: str
    status: PartLabel
    reason: str


class DimensionScore(Entity):
    score: int = Field(ge=1, le=5)
    reason: str


class ReportVerdict(Entity):
    """Coverage per required part and the three 1-5 scores; on a judge failure all are empty
    and ``error`` says why."""

    parts: list[PartVerdict] = []
    quantitative_reasoning: DimensionScore | None = None
    synthesis: DimensionScore | None = None
    clarity: DimensionScore | None = None
    error: str | None = None


class JudgeAnswer(BaseModel):
    """A judge answer as the model sends it: camelCase, extra keys ignored."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class _ClaimJudgement(JudgeAnswer):
    id: str
    verdict: ClaimLabel
    reason: str = Field(description=f"At most {REASON_WORDS} words.")


class _ClaimJudgements(JudgeAnswer):
    verdicts: list[_ClaimJudgement]


class _PartJudgement(JudgeAnswer):
    part: int = Field(description="The required part's number.")
    status: PartLabel
    reason: str = Field(description=f"At most {REASON_WORDS} words.")


class _Dimension(JudgeAnswer):
    score: int = Field(ge=1, le=5)
    reason: str = Field(description=f"At most {REASON_WORDS} words.")


class _ReportJudgement(JudgeAnswer):
    coverage: list[_PartJudgement]
    quantitative_reasoning: _Dimension
    synthesis: _Dimension
    clarity: _Dimension


def judge_claims(
    claims: list[ClaimEvidence], *, model: ModelClient, batch_size: int = 8
) -> list[ClaimVerdict]:
    """One verdict per claim, in order; a batch the judge cannot answer is ``judge_error``."""
    verdicts: list[ClaimVerdict] = []
    for start in range(0, len(claims), batch_size):
        verdicts.extend(_judge_batch(claims[start : start + batch_size], model))
    return verdicts


def judge_report(
    question: str, required_parts: list[str], report_text: str, *, model: ModelClient
) -> ReportVerdict:
    """Coverage of each required part and the 1-5 dimension scores of one report."""

    def check(answer: _ReportJudgement) -> None:
        _expect_ids([str(p.part) for p in answer.coverage], len(required_parts), "part")

    try:
        answer = ask_judge(
            model,
            REPORT_PROMPT,
            _report_input(question, required_parts, report_text),
            _ReportJudgement,
            check,
        )
    except (ModelError, ValueError) as exc:
        logger.warning("report judge failed: %s", type(exc).__name__)
        return ReportVerdict(error=failure_text(exc))
    statuses = {p.part: p for p in answer.coverage}
    return ReportVerdict(
        parts=[
            PartVerdict(part=text, status=statuses[n].status, reason=statuses[n].reason)
            for n, text in enumerate(required_parts, 1)
        ],
        quantitative_reasoning=_dimension(answer.quantitative_reasoning),
        synthesis=_dimension(answer.synthesis),
        clarity=_dimension(answer.clarity),
    )


def _judge_batch(batch: list[ClaimEvidence], model: ModelClient) -> list[ClaimVerdict]:
    """Claims are numbered within the batch, so ids that repeat across runs cannot collide."""

    def check(answer: _ClaimJudgements) -> None:
        _expect_ids([v.id for v in answer.verdicts], len(batch), "claim")

    try:
        answer = ask_judge(model, CLAIMS_PROMPT, _claims_input(batch), _ClaimJudgements, check)
    except (ModelError, ValueError) as exc:
        logger.warning("claim judge failed for %d claims: %s", len(batch), type(exc).__name__)
        reason = failure_text(exc)
        return [
            ClaimVerdict(claim_id=c.claim_id, verdict=JUDGE_ERROR, reason=reason) for c in batch
        ]
    by_number = {v.id: v for v in answer.verdicts}
    return [
        ClaimVerdict(
            claim_id=claim.claim_id,
            verdict=by_number[str(n)].verdict,
            reason=by_number[str(n)].reason,
        )
        for n, claim in enumerate(batch, 1)
    ]


def ask_judge[AnswerT: JudgeAnswer](
    model: ModelClient,
    prompt: str,
    user_text: str,
    answer_type: type[AnswerT],
    check: Callable[[AnswerT], None],
) -> AnswerT:
    """One judge call, its answer read leniently, then checked; a schema failure is sent back
    once with what was wrong. Raises ``ModelError`` or ``ValueError`` when it still fails."""
    try:
        return _call(model, prompt, user_text, answer_type, check)
    except ValueError as rejected:
        reason = one_line(rejected) if isinstance(rejected, ValidationError) else str(rejected)
        logger.warning("judge answer rejected, retrying once: %s", reason)
    retry = f"{user_text}\n\nYour previous answer was rejected: {reason}. Answer again in full."
    return _call(model, prompt, retry, answer_type, check)


def _call[AnswerT: JudgeAnswer](
    model: ModelClient,
    prompt: str,
    user_text: str,
    answer_type: type[AnswerT],
    check: Callable[[AnswerT], None],
) -> AnswerT:
    request = ModelRequest(
        stage=Stage.JUDGE,
        # A placeholder: the judge client (``evals.judge_client``) sends each call to the judge
        # stage's model and fallbacks, with that stage's token cap.
        model=Stage.JUDGE.value,
        system=[load_prompt(prompt)],
        messages=[HumanMessage(user_text)],
        output_schema=answer_type,
    )
    answer = answer_type.model_validate(_lenient(model.invoke(request).structured))
    check(answer)
    return answer


def _lenient(answer: dict[str, Any] | None) -> dict[str, Any]:
    """The slips open-weight models make with a result schema, fixed before validation: a list
    sent as a JSON string is decoded (DeepSeek v3.2 does this with the planner's tasks), labels
    are lower-cased, and reasons are cut to ``REASON_WORDS`` words, in list items, nested
    objects and the answer itself."""
    if not isinstance(answer, dict):
        raise ValueError("the judge returned no structured answer")
    fixed = dict(answer)
    for key, value in answer.items():
        if isinstance(value, str) and value.lstrip().startswith(("[", "{")):
            try:
                fixed[key] = value = json.loads(value)
            except json.JSONDecodeError:
                pass
        if isinstance(value, list):
            fixed[key] = [_fixed_item(item) for item in value]
        elif isinstance(value, dict):
            fixed[key] = _fixed_item(value)
    return dict(_fixed_item(fixed))


def _fixed_item(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    fixed = dict(item)
    for key in ("verdict", "status"):
        if isinstance(fixed.get(key), str):
            fixed[key] = fixed[key].strip().lower()
    if "id" in fixed:
        fixed["id"] = str(fixed["id"])
    if isinstance(fixed.get("reason"), str):
        fixed["reason"] = " ".join(fixed["reason"].split()[:REASON_WORDS])
    return fixed


def _expect_ids(got: list[str], count: int, noun: str) -> None:
    """Each of 1..count exactly once, so no item is dropped or judged twice."""
    expected = [str(n) for n in range(1, count + 1)]
    if sorted(got, key=_number_order) != expected:
        raise ValueError(
            f"expected one {noun} verdict for each of {', '.join(expected)}; got {got}"
        )


def _number_order(text: str) -> tuple[int, str]:
    return (int(text), "") if text.isdigit() else (0, text)


def _dimension(answer: _Dimension) -> DimensionScore:
    return DimensionScore(score=answer.score, reason=answer.reason)


def failure_text(exc: Exception) -> str:
    detail = one_line(exc) if isinstance(exc, ValidationError) else str(exc)
    return f"{type(exc).__name__}: {detail}"[:300]


def _claims_input(batch: list[ClaimEvidence]) -> str:
    items = [
        {
            "id": str(n),
            "claim": claim.claim,
            "citedUrl": claim.cited_url,
            "excerpt": claim.excerpt,
            "context": claim.context,
        }
        for n, claim in enumerate(batch, 1)
    ]
    # "<" and ">" escaped as JSON unicode escapes: page text cannot close the block.
    data = json.dumps(items, ensure_ascii=False, indent=1)
    data = data.replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<claims_data>\n{data}\n</claims_data>"


def cut_report(report_text: str) -> str:
    """The report, cut at ``REPORT_MAX_CHARS`` with the cut marked."""
    if len(report_text) <= REPORT_MAX_CHARS:
        return report_text
    return report_text[:REPORT_MAX_CHARS] + "\n[report cut here for length]"


def _report_input(question: str, required_parts: list[str], report_text: str) -> str:
    report = cut_report(report_text)
    parts = "\n".join(f"{n}. {untrusted(part)}" for n, part in enumerate(required_parts, 1))
    return (
        f"<question>\n{untrusted(question)}\n</question>\n"
        f"<required_parts>\n{parts}\n</required_parts>\n"
        f"<report>\n{untrusted(report)}\n</report>"
    )


def untrusted(text: str) -> str:
    """``text`` unable to close its block or forge a tag."""
    return text.replace("<", "&lt;").replace(">", "&gt;")
