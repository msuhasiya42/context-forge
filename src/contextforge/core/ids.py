"""Identifier and timestamp helpers shared by every entity."""

from datetime import UTC, datetime

from ulid import ULID


def new_ulid() -> str:
    """Return a new ULID string; within one process, lexical order is creation order."""
    return str(ULID())


def now_iso() -> str:
    """Return the current UTC time as ISO 8601 with second precision and a ``Z`` suffix."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
