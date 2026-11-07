"""Identifier and timestamp helpers shared by every entity."""

import hashlib
from datetime import UTC, datetime

from ulid import ULID


def new_ulid() -> str:
    """Return a new ULID string; within one process, lexical order is creation order."""
    return str(ULID())


def derived_id(key: str) -> str:
    """Return a ULID-shaped id that is a pure function of ``key``.

    For records a re-run may write again (a replayed tool call, a repeated clarification): the
    same key gives the same id, so the second write overwrites instead of duplicating. The
    timestamp bits are hash bits, so derived ids do not sort by creation time.
    """
    return str(ULID.from_bytes(hashlib.sha256(key.encode()).digest()[:16]))


def now_iso() -> str:
    """Return the current UTC time as ISO 8601 with second precision and a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
