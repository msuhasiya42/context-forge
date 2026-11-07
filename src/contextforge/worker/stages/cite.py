"""Validate web evidence against its saved snapshot; dataset verification is separate.

A web snapshot (a raw_result Artifact) is cited with a url locator naming a URL the snapshot
holds and a verbatim excerpt of that URL's text: a fetched page's text or a search hit's
snippet. The excerpt must have ``MIN_EXCERPT_CHARS`` characters and is matched with whitespace
collapsed and case folded on both sides. A fact failing that is dropped, and the reason is
kept as an uncertainty, never dropped silently.
"""

import json
import logging
from typing import Any, TypedDict

from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus
from contextforge.core.state import Fact, RequestState
from contextforge.core.values import EvidenceRef, LocatorKind
from contextforge.worker.deps import WorkerDeps

logger = logging.getLogger(__name__)

# Shorter quotes ("e", "Python") occur on almost any page and prove nothing. The web tools'
# descriptions tell the model this minimum.
MIN_EXCERPT_CHARS = 20


class Citations(TypedDict):
    """The facts that passed, and one uncertainty per distinct reason a fact was dropped."""

    facts: list[Fact]
    uncertainties: list[str]


def check_citations(brief: RequestState, deps: WorkerDeps) -> Citations:
    facts: list[Fact] = []
    uncertainties: list[str] = []
    for fact in brief["facts"]:
        problem = _problem(fact, brief, deps)
        if problem is None:
            facts.append(fact)
        else:
            uncertainties.append(problem)
    return Citations(facts=facts, uncertainties=list(dict.fromkeys(uncertainties)))


def cite_stage(brief: RequestState, deps: WorkerDeps) -> list[Fact]:
    """The supported facts. Each dropped fact's uncertainty is logged as a warning until the
    workflow passes ``check_citations``' uncertainties on to the report."""
    citations = check_citations(brief, deps)
    for uncertainty in citations["uncertainties"]:
        logger.warning("dropped a fact: %s", uncertainty)
    return citations["facts"]


def _problem(fact: Fact, brief: RequestState, deps: WorkerDeps) -> str | None:
    """Why ``fact`` is unsupported, worded as the uncertainty to record; None if supported."""
    refs = [EvidenceRef.model_validate(raw) for raw in fact["evidence_refs"]]
    if not refs:
        return f"unsupported claim without evidence: {fact['text'][:80]}"
    for ref in refs:
        problem = _ref_problem(ref, brief, deps)
        if problem is not None:
            return problem
    return None


def _ref_problem(ref: EvidenceRef, brief: RequestState, deps: WorkerDeps) -> str | None:
    artifact = deps.repos.artifacts.get(brief["chat_id"], ref.artifact_id)
    if ref.locator.kind != LocatorKind.URL:
        # Dataset evidence is verified elsewhere; a web snapshot only through its URL.
        if artifact is not None and artifact.kind == ArtifactKind.RAW_RESULT:
            return (
                f"unsupported excerpt for artifact {ref.artifact_id}: "
                "web evidence needs a url locator"
            )
        return None
    url = ref.locator.value
    evidence = _snapshot_text(artifact, url, brief, deps)
    if evidence is None or not _quotes(ref.excerpt, evidence):
        return f"unsupported excerpt for {url}"
    return None


def _snapshot_text(
    artifact: Artifact | None, url: str, brief: RequestState, deps: WorkerDeps
) -> str | None:
    """The text ``artifact`` saved for ``url``; None unless it is this user's available
    snapshot holding that URL."""
    if (
        artifact is None
        or artifact.status != ArtifactStatus.AVAILABLE
        or artifact.user_id != brief["user_id"]
    ):
        return None
    try:
        records = _records(deps.blobs.get(artifact.storage_ref))
    except (LookupError, ValueError):  # gone (expired) or not a snapshot
        return None
    for record in records:
        if record.get("url") == url:
            return str(record.get("text", record.get("snippet", "")))
    return None


def _records(data: bytes) -> list[dict[str, Any]]:
    """A snapshot's records: one JSON object (a page) or JSON lines (search rows).

    Split on newline bytes only: JSON escapes newlines inside strings, but a page's text may
    hold other characters that ``str.splitlines`` would split on.
    """
    records = [json.loads(line) for line in data.split(b"\n") if line.strip()]
    return [record for record in records if isinstance(record, dict)]


def _quotes(excerpt: str | None, evidence: str) -> bool:
    """True when ``excerpt`` is long enough to mean something and appears in ``evidence``."""
    wanted = _normalised(excerpt or "")
    return len(wanted) >= MIN_EXCERPT_CHARS and wanted in _normalised(evidence)


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()
