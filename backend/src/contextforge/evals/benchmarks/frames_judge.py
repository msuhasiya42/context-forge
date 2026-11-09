"""FRAMES answer extraction and grading by the offline judge (the judge stage's model).

The research pipeline and its prompts are unchanged: a finished report is read afterwards. One
call extracts the shortest answer the report states and the claims it rests on; a second grades
it against the gold answer, following the FRAMES paper's autorater (Krishna et al. 2024,
arXiv:2409.12941, section 3 and appendix): the meaning and vital facts of the gold answer must
be present, in any equivalent form.
"""

import logging
from typing import Literal

from pydantic import Field

from contextforge.core.entities import Entity
from contextforge.core.entities.message import Claim
from contextforge.core.models.base import ModelClient
from contextforge.core.models.errors import ModelError
from contextforge.evals.judge import (
    JUDGE_ERROR,
    REASON_WORDS,
    JudgeAnswer,
    ask_judge,
    cut_report,
    failure_text,
    untrusted,
)

logger = logging.getLogger(__name__)

EXTRACT_PROMPT = "judge_frames_extract.1"
GRADE_PROMPT = "judge_frames.1"

AnswerVerdict = Literal["correct", "incorrect", "no_answer"]


class ExtractedAnswer(Entity):
    """The report's final answer (None when it gives none) and the ids of the claims that state
    it or the facts it was derived from; ``error`` is set when the judge could not answer."""

    answer: str | None
    claim_ids: list[str] = []
    error: str | None = None


class AnswerGrade(Entity):
    verdict: AnswerVerdict | Literal["judge_error"]
    # The extracted answer is wrong for 2024 but plausible for a later date.
    possibly_stale: bool = False
    reason: str


class _Extraction(JudgeAnswer):
    # Models often send a numeric answer as a JSON number.
    answer: str | int | float | None = Field(
        description="The shortest answer the report states, or null."
    )
    claims: list[int] = Field(description="Numbers of the claims the answer rests on.")


class _Grading(JudgeAnswer):
    verdict: AnswerVerdict
    possibly_stale: bool = False
    reason: str = Field(description=f"At most {REASON_WORDS} words.")


def extract_final_answer(
    question: str, report_text: str, *, model: ModelClient, claims: list[Claim] | None = None
) -> ExtractedAnswer:
    """The shortest answer to ``question`` that the report states, or None when it gives none,
    with the ``claims`` (numbered for the judge) that state it or what it was derived from."""
    claims = claims or []

    def check(answer: _Extraction) -> None:
        unknown = [n for n in answer.claims if not 1 <= n <= len(claims)]
        if unknown:
            raise ValueError(f"claim numbers {unknown} are not in 1..{len(claims)}")

    try:
        answer = ask_judge(
            model, EXTRACT_PROMPT, _extract_input(question, report_text, claims), _Extraction, check
        )
    except (ModelError, ValueError) as exc:
        logger.warning("answer extraction failed: %s", type(exc).__name__)
        return ExtractedAnswer(answer=None, error=failure_text(exc))
    text = "" if answer.answer is None else str(answer.answer).strip()
    return ExtractedAnswer(
        answer=text or None,
        claim_ids=[claims[n - 1].claim_id for n in dict.fromkeys(answer.claims)] if text else [],
    )


def grade_answer(
    question: str, gold: str, extracted: str | None, *, model: ModelClient
) -> AnswerGrade:
    """``correct`` when ``extracted`` means the gold answer, ``incorrect`` when it does not,
    ``no_answer`` when there is none (no call is made) or it declines to answer."""
    if extracted is None:
        return AnswerGrade(verdict="no_answer", reason="The report gives no answer.")
    try:
        answer = ask_judge(
            model, GRADE_PROMPT, _grade_input(question, gold, extracted), _Grading, lambda _: None
        )
    except (ModelError, ValueError) as exc:
        logger.warning("answer grading failed: %s", type(exc).__name__)
        return AnswerGrade(verdict=JUDGE_ERROR, reason=failure_text(exc))
    return AnswerGrade(
        verdict=answer.verdict,
        # Staleness only explains a mismatch; a correct answer is not stale.
        possibly_stale=answer.possibly_stale and answer.verdict == "incorrect",
        reason=answer.reason,
    )


def _extract_input(question: str, report_text: str, claims: list[Claim]) -> str:
    numbered = "\n".join(f"{n}. {untrusted(c.text)}" for n, c in enumerate(claims, 1))
    return (
        f"<question>\n{untrusted(question)}\n</question>\n"
        f"<report>\n{untrusted(cut_report(report_text))}\n</report>\n"
        f"<claims>\n{numbered}\n</claims>"
    )


def _grade_input(question: str, gold: str, extracted: str) -> str:
    return (
        f"<question>\n{untrusted(question)}\n</question>\n"
        f"<gold_answer>\n{untrusted(gold)}\n</gold_answer>\n"
        f"<predicted_answer>\n{untrusted(extracted)}\n</predicted_answer>"
    )
