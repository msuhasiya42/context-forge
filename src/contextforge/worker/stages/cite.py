"""Validate web evidence against the immutable snapshot; dataset verification is separate."""

import json

from contextforge.core.entities.artifact import ArtifactStatus
from contextforge.core.state import Fact, RequestState
from contextforge.core.values import EvidenceRef, LocatorKind
from contextforge.worker.deps import WorkerDeps


def cite_stage(brief: RequestState, deps: WorkerDeps) -> list[Fact]:
    return [fact for fact in brief["facts"] if _supported(fact, brief, deps)]


def _supported(fact: Fact, brief: RequestState, deps: WorkerDeps) -> bool:
    for raw in fact["evidence_refs"]:
        ref = EvidenceRef.model_validate(raw)
        if ref.locator.kind != LocatorKind.URL:
            continue
        artifact = deps.repos.artifacts.get(brief["chat_id"], ref.artifact_id)
        if (
            artifact is None
            or artifact.status != ArtifactStatus.AVAILABLE
            or artifact.user_id != brief["user_id"]
        ):
            return False
        try:
            snapshot = json.loads(deps.blobs.get(artifact.storage_ref))
        except (LookupError, ValueError):
            return False
        if snapshot.get("url") != ref.locator.value:
            return False
        evidence = snapshot.get("text", snapshot.get("snippet", ""))
        if not ref.excerpt or ref.excerpt not in evidence:
            return False
    return bool(fact["evidence_refs"])
