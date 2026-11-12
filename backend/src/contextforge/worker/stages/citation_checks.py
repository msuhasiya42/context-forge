"""Validate web evidence against its saved snapshot; dataset verification is separate.

A web snapshot (a raw_result Artifact) is cited with a url locator naming a URL the snapshot
holds and a verbatim excerpt of that URL's text: a fetched page's text or a search hit's
snippet. The excerpt must have ``MIN_EXCERPT_CHARS`` characters and is matched with whitespace
collapsed, case folded and pictographs left out on both sides, ignoring a leading or trailing
ellipsis. A quote differing only in sentence-ending periods can be replaced with the actual
contiguous source span; changed words, numbers and operators are not repaired. A ref that
quotes no excerpt is checked with the fact's own text as its quote: a fact copied word for
word from its source is its own evidence, and anything else is dropped. A web fact
cited with another locator (a model wrote
``{"kind": "row", "value": "0"}`` for a search hit) is pointed at the URL whose saved text holds
its excerpt. A fact failing all that is dropped, and the reason is kept as an uncertainty,
never dropped silently. Facts stating the same claim are merged by
``merge_duplicate_findings``.

A conclusion quotes no source, so cite checks that all the facts it rests on passed
(``supported_conclusions``).
"""

import json
import re
import unicodedata
from collections.abc import Iterable
from typing import Any, NotRequired, TypedDict

from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus
from contextforge.core.state import Conclusion, Fact, RequestState
from contextforge.core.values import EvidenceRef, Locator, LocatorKind
from contextforge.worker.dependencies import WorkerDependencies

# Shorter quotes ("e", "Python") occur on almost any page and prove nothing. The web tools'
# descriptions tell the model this minimum.
MIN_EXCERPT_CHARS = 20


class CitationCheckResult(TypedDict):
    """The facts that passed, and one uncertainty per distinct reason a fact was dropped."""

    facts: list[Fact]
    uncertainties: list[str]
    review: NotRequired[dict[str, Any]]


def check_citations(
    request_state: RequestState, worker_dependencies: WorkerDependencies
) -> CitationCheckResult:
    """The request state's facts that cite their evidence correctly, each web ref naming the URL it
    quotes, with repeats merged; the reasons for the rest go to the report as uncertainties.

    Each fact is checked before merging, so one repeat's bad ref cannot drop the claim."""
    facts: list[Fact] = []
    uncertainties: list[str] = []
    for fact in request_state["facts"]:
        checked = _checked(fact, request_state, worker_dependencies)
        if isinstance(checked, str):
            uncertainties.append(checked)
        else:
            facts.append(checked)
    return CitationCheckResult(
        facts=merge_duplicate_findings(facts), uncertainties=list(dict.fromkeys(uncertainties))
    )


def merge_duplicate_findings(facts: list[Fact]) -> list[Fact]:
    """One fact per claim, first occurrence first, with the first one's text and every repeat's
    evidence refs (F7 ruling 3: a comparison task restates its dependencies' facts, "350+ kms."
    beside "350+ km.")."""
    groups: dict[str, list[Fact]] = {}
    for fact in facts:
        groups.setdefault(claim_key(fact["text"]), []).append(fact)
    return [
        Fact(text=group[0]["text"], evidence_refs=_union(f["evidence_refs"] for f in group))
        for group in groups.values()
    ]


def supported_conclusions(
    conclusions: list[Conclusion], facts: list[Fact]
) -> tuple[list[Conclusion], list[str]]:
    """Retain a conclusion only when all its declared supporting findings survived.

    Silently shrinking a comparison's evidence from two dates to one does not establish
    which is newer. Missing support becomes a limitation instead of a confident assessment.
    """
    passed = {claim_key(fact["text"]): fact["text"] for fact in facts}
    kept: list[Conclusion] = []
    dropped: list[str] = []
    for conclusion in conclusions:
        keys = dict.fromkeys(claim_key(text) for text in conclusion["based_on"])
        based_on = [passed[key] for key in keys if key in passed]
        if not based_on:
            dropped.append(
                f"unsupported conclusion, no finding it rests on held: {conclusion['text'][:80]}"
            )
            continue
        if len(based_on) != len(keys):
            dropped.append(
                "unsupported conclusion, required supporting findings did not all pass: "
                + conclusion["text"][:80]
            )
            continue
        kept.append(
            Conclusion(
                text=conclusion["text"], based_on=based_on, confidence=conclusion["confidence"]
            )
        )
    return kept, dropped


def claim_key(text: str) -> str:
    """``text`` as two facts are compared for stating the same claim: normalised as excerpts are
    (case, whitespace, pictographs), each punctuation mark read as a space, and a unit's plural
    read as the unit ("kms" as "km", "hours" as "hour").

    A decimal point, thousands separator or minus sign before a digit, and a percent sign, are
    kept: "3.5" and "35", or "-5" and "5", are different claims.
    """
    text = _normalised(text)
    spaced = "".join(" " if _is_separator(text, i) else char for i, char in enumerate(text))
    return " ".join(_unit_singular(word) for word in spaced.split())


def _is_separator(text: str, index: int) -> bool:
    """Whether the character at ``index`` is punctuation that is not part of a number."""
    char = text[index]
    if char == "%" or not unicodedata.category(char).startswith("P"):
        return False
    return not (char in ".,-" and text[index + 1 : index + 2].isdigit())


# Units a fact may write in the plural; "kms" and "km" state the same distance. Ambiguous
# words are left out: "ms" is milliseconds, not metres, and "second" is also an ordinal.
_UNITS = frozenset(
    "km kilometre kilometer mile metre meter kg hr hour min minute day week month yr year "
    "degree".split()
)


def _unit_singular(word: str) -> str:
    unit = word.removesuffix("s")
    return unit if unit != word and unit in _UNITS else word


def _union(ref_lists: Iterable[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """The refs of every list, each once, first occurrence first (refs compare by value)."""
    union: list[dict[str, Any]] = []
    for refs in ref_lists:
        for ref in refs:
            if ref not in union:
                union.append(ref)
    return union


def _checked(
    fact: Fact, request_state: RequestState, worker_dependencies: WorkerDependencies
) -> Fact | str:
    """``fact`` with every web ref naming the URL it quotes, or why it is unsupported, worded
    as the uncertainty to record. A fact whose refs all stood as written is returned as is."""
    refs = [EvidenceRef.model_validate(raw) for raw in fact["evidence_refs"]]
    if not refs:
        return f"unsupported claim without evidence: {fact['text'][:80]}"
    checked: list[EvidenceRef] = []
    for ref in refs:
        result = _checked_ref(ref, fact["text"], request_state, worker_dependencies)
        if isinstance(result, str):
            return result
        checked.append(result)
    if checked == refs:
        return fact
    dumped = [ref.model_dump(mode="json", exclude_none=True) for ref in checked]
    return Fact(text=fact["text"], evidence_refs=dumped)


def _checked_ref(
    ref: EvidenceRef,
    fact_text: str,
    request_state: RequestState,
    worker_dependencies: WorkerDependencies,
) -> EvidenceRef | str:
    artifact = worker_dependencies.repositories.artifacts.get(
        request_state["chat_id"], ref.artifact_id
    )
    if ref.locator.kind != LocatorKind.URL:
        # Dataset evidence is verified elsewhere; a web snapshot only through a URL it holds.
        if artifact is None or artifact.kind != ArtifactKind.RAW_RESULT:
            return ref
        quoted = _quoting(ref, fact_text)
        url = _url_quoting(
            quoted.excerpt, _owned_records(artifact, request_state, worker_dependencies)
        )
        if url is None:
            return (
                f"unsupported excerpt for artifact {ref.artifact_id}: "
                f"no page or search hit it saved holds it{_unquoted(ref)}"
            )
        return quoted.model_copy(update={"locator": Locator(kind=LocatorKind.URL, value=url)})
    url = ref.locator.value
    evidence = _text_for(url, _owned_records(artifact, request_state, worker_dependencies))
    if evidence is None:
        return f"unsupported excerpt for {url}"
    quoted = _quoting(ref, fact_text)
    if _quotes(quoted.excerpt, evidence):
        return quoted
    recovered = _recover_sentence_periods(quoted.excerpt, evidence)
    if recovered is None:
        return f"unsupported excerpt for {url}{_unquoted(ref)}"
    # Store actual source characters, not the model's punctuated reconstruction. The
    # later semantic review and exported evaluation still receive a real source excerpt.
    return quoted.model_copy(update={"excerpt": recovered})


def _quoting(ref: EvidenceRef, fact_text: str) -> EvidenceRef:
    """``ref``, or, when it quotes no excerpt, ``ref`` quoting the fact's own text. Only a web
    check uses it, so a ref is stored with that quote only once its source is shown to hold it."""
    return ref if ref.excerpt else ref.model_copy(update={"excerpt": fact_text})


def _unquoted(ref: EvidenceRef) -> str:
    """Why a ref without an excerpt failed, added to its uncertainty."""
    return (
        "" if ref.excerpt else " (no excerpt quoted; the finding's own text is not in the source)"
    )


def _owned_records(
    artifact: Artifact | None, request_state: RequestState, worker_dependencies: WorkerDependencies
) -> list[dict[str, Any]]:
    """The records ``artifact`` saved; none unless it is this user's available snapshot."""
    if (
        artifact is None
        or artifact.status != ArtifactStatus.AVAILABLE
        or artifact.user_id != request_state["user_id"]
    ):
        return []
    try:
        return _records(worker_dependencies.blobs.get(artifact.storage_ref))
    except (LookupError, ValueError):  # gone (expired) or not a snapshot
        return []


def _record_text(record: dict[str, Any]) -> str:
    """A page's text or a search hit's snippet."""
    return str(record.get("text", record.get("snippet", "")))


def _text_for(url: str, records: list[dict[str, Any]]) -> str | None:
    """The text saved for ``url``; None when no record holds that URL."""
    for record in records:
        if record.get("url") == url:
            return _record_text(record)
    return None


def _url_quoting(excerpt: str | None, records: list[dict[str, Any]]) -> str | None:
    """The URL of the first record whose text holds ``excerpt``; None when none does."""
    for record in records:
        url = record.get("url")
        if isinstance(url, str) and _quotes(excerpt, _record_text(record)):
            return url
    return None


def _records(data: bytes) -> list[dict[str, Any]]:
    """A snapshot's records: one JSON object (a page) or JSON lines (search rows).

    Split on newline bytes only: JSON escapes newlines inside strings, but a page's text may
    hold other characters that ``str.splitlines`` would split on.
    """
    records = [json.loads(line) for line in data.split(b"\n") if line.strip()]
    return [record for record in records if isinstance(record, dict)]


def _quotes(excerpt: str | None, evidence: str) -> bool:
    """True when ``excerpt`` is long enough to mean something and appears in ``evidence``.

    A leading or trailing ellipsis marks text left out, not text quoted: a search snippet ends
    "... six sessions ..." and a model quotes it as "six sessions..." (live run 3).
    """
    wanted = _ELLIPSIS_ENDS.sub("", _normalised(excerpt or ""))
    return len(wanted) >= MIN_EXCERPT_CHARS and wanted in _normalised(evidence)


def _recover_sentence_periods(excerpt: str | None, evidence: str) -> str | None:
    """Recover a contiguous source span when only sentence-ending periods differ.

    Live Python release pages put the date and description on adjacent lines without
    periods. Models appended periods to both. Words, order, internal decimal/version
    points, signs and all other punctuation must still match; this is not fuzzy quoting.
    """
    wanted = _ELLIPSIS_ENDS.sub("", _normalised(excerpt or ""))
    if len(wanted) < MIN_EXCERPT_CHARS:
        return None
    words = [_sentence_word(word) for word in wanted.split()]
    spans = [
        (match.start(), match.end(), word)
        for match in re.finditer(r"\S+", evidence)
        if (word := _normalised(match.group()))
    ]
    source_words = [_sentence_word(word) for _, _, word in spans]
    for start in range(len(spans) - len(words) + 1):
        if source_words[start : start + len(words)] == words:
            recovered = evidence[spans[start][0] : spans[start + len(words) - 1][1]]
            if len(_normalised(recovered)) >= MIN_EXCERPT_CHARS:
                return recovered
    return None


def _sentence_word(word: str) -> str:
    # A single final period only: never erase an internal decimal or an omitted span (...).
    return word[:-1] if len(word) > 1 and word.endswith(".") and not word.endswith("..") else word


# "..." or "…" with the spaces beside it, at either end of a normalised excerpt.
_ELLIPSIS_ENDS = re.compile(r"^(?:\.\.\.\s*)+|(?:\s*\.\.\.)+$")


# Pictographs and invisible format characters ("🔹", a variation selector, a zero-width
# joiner) carry no claim; a model quoting "Higher! 🔹 Sensex: +3.11%" leaves them out (live
# run 4). Letters, digits, currency signs and punctuation still have to match.
_IGNORED_CATEGORIES = frozenset({"So", "Cf"})
_VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})


def _normalised(text: str) -> str:
    """Whitespace collapsed, case folded, "…" written as "...", pictographs left out."""
    kept = "".join(
        char
        for char in text.replace("…", "...")
        if unicodedata.category(char) not in _IGNORED_CATEGORIES
        and char not in _VARIATION_SELECTORS
    )
    return " ".join(kept.split()).casefold()
