"""Blob storage boundary: the only place, with ``core/repositories``, that builds storage refs."""

from typing import Protocol

MEMORY_SCHEME = "mem://"


class BlobStore(Protocol):
    def put(self, key: str, data: bytes) -> str:
        """Store ``data`` under ``key`` (overwriting) and return its ``storage_ref``."""
        ...

    def get(self, ref: str) -> bytes:
        """Return the bytes behind a ``storage_ref`` returned by ``put``."""
        ...


class MemoryBlobStore:
    """Process-local BlobStore; refs look like ``mem://<key>``."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, data: bytes) -> str:
        self._blobs[key] = data
        return f"{MEMORY_SCHEME}{key}"

    def get(self, ref: str) -> bytes:
        if not ref.startswith(MEMORY_SCHEME):
            raise ValueError(f"not a memory blob ref: {ref!r}")
        key = ref.removeprefix(MEMORY_SCHEME)
        try:
            return self._blobs[key]
        except KeyError:
            raise LookupError(f"no blob stored at {ref!r}") from None
