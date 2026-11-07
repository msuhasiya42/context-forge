"""Validate web evidence against its saved snapshot; dataset verification is separate.

A web snapshot (a raw_result Artifact) is cited with a url locator naming a URL the snapshot
holds and a verbatim excerpt of that URL's text: a fetched page's text or a search hit's
snippet. The excerpt must have ``MIN_EXCERPT_CHARS`` characters and is matched with whitespace
collapsed, case folded and pictographs left out on both sides, ignoring a leading or trailing
ellipsis. A web fact cited with another locator (a model wrote
``{"kind": "row", "value": "0"}`` for a search hit) is pointed at the URL whose saved text holds
its excerpt. A fact failing all that is dropped, and the reason is kept as an uncertainty,
never dropped silently.
"""

import json
import re
import unicodedata
from typing import Any, TypedDict

from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus
from contextforge.core.state import Fact, RequestState
from contextforge.core.values import EvidenceRef, Locator, LocatorKind
from contextforge.worker.deps import WorkerDeps

# Shorter quotes ("e", "Python") occur on almost any page and prove nothing. The web tools'
# descriptions tell the model this minimum.
MIN_EXCERPT_CHARS = 20


class Citations(TypedDict):
    """The facts that passed, and one uncertainty per distinct reason a fact was dropped."""

    facts: list[Fact]
    uncertainties: list[str]


def check_citations(brief: RequestState, deps: WorkerDeps) -> Citations:
    """The brief's facts that cite their evidence correctly, each web ref naming the URL it
    quotes; the reasons for the rest go to the report as uncertainties."""
    facts: list[Fact] = []
    uncertainties: list[str] = []
    for fact in brief["facts"]:
        checked = _checked(fact, brief, deps)
        if isinstance(checked, str):
            uncertainties.append(checked)
        else:
            facts.append(checked)
    return Citations(facts=facts, uncertainties=list(dict.fromkeys(uncertainties)))


def _checked(fact: Fact, brief: RequestState, deps: WorkerDeps) -> Fact | str:
    """``fact`` with every web ref naming the URL it quotes, or why it is unsupported, worded
    as the uncertainty to record. A fact whose refs all stood as written is returned as is."""
    refs = [EvidenceRef.model_validate(raw) for raw in fact["evidence_refs"]]
    if not refs:
        return f"unsupported claim without evidence: {fact['text'][:80]}"
    checked: list[EvidenceRef] = []
    for ref in refs:
        result = _checked_ref(ref, brief, deps)
        if isinstance(result, str):
            return result
        checked.append(result)
    if checked == refs:
        return fact
    dumped = [ref.model_dump(mode="json", exclude_none=True) for ref in checked]
    return Fact(text=fact["text"], evidence_refs=dumped)


def _checked_ref(ref: EvidenceRef, brief: RequestState, deps: WorkerDeps) -> EvidenceRef | str:
    artifact = deps.repos.artifacts.get(brief["chat_id"], ref.artifact_id)
    if ref.locator.kind != LocatorKind.URL:
        # Dataset evidence is verified elsewhere; a web snapshot only through a URL it holds.
        if artifact is None or artifact.kind != ArtifactKind.RAW_RESULT:
            return ref
        url = _url_quoting(ref.excerpt, _owned_records(artifact, brief, deps))
        if url is None:
            return (
                f"unsupported excerpt for artifact {ref.artifact_id}: "
                "no page or search hit it saved holds it"
            )
        return ref.model_copy(update={"locator": Locator(kind=LocatorKind.URL, value=url)})
    url = ref.locator.value
    evidence = _text_for(url, _owned_records(artifact, brief, deps))
    if evidence is None or not _quotes(ref.excerpt, evidence):
        return f"unsupported excerpt for {url}"
    return ref


def _owned_records(
    artifact: Artifact | None, brief: RequestState, deps: WorkerDeps
) -> list[dict[str, Any]]:
    """The records ``artifact`` saved; none unless it is this user's available snapshot."""
    if (
        artifact is None
        or artifact.status != ArtifactStatus.AVAILABLE
        or artifact.user_id != brief["user_id"]
    ):
        return []
    try:
        return _records(deps.blobs.get(artifact.storage_ref))
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
