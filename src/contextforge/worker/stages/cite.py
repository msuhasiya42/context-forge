"""Cite stage: the facts the report turns into claims."""

from contextforge.core.state import Fact, RequestState
from contextforge.worker.deps import WorkerDeps


def cite_stage(brief: RequestState, deps: WorkerDeps) -> list[Fact]:
    """Phase 1 stub: ``brief["facts"]`` unchanged.

    Phase 3 replaces the body with the evidence re-read and the claim call, which use ``deps``.
    """
    return brief["facts"]
