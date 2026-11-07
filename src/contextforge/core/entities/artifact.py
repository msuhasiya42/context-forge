"""Artifact entity (implementation plan section 3.6). Immutable: a re-upload is a new Artifact."""

from enum import StrEnum

from contextforge.core.entities import Entity


class ArtifactKind(StrEnum):
    UPLOAD = "upload"
    DATASET = "dataset"
    RAW_RESULT = "raw_result"
    DELIVERABLE = "deliverable"


class ArtifactStatus(StrEnum):
    UPLOADING = "UPLOADING"
    AVAILABLE = "AVAILABLE"
    FAILED = "FAILED"
    DELETED = "DELETED"


class IndexStatus(StrEnum):
    NOT_INDEXED = "NOT_INDEXED"
    PENDING = "PENDING"
    INDEXED = "INDEXED"
    FAILED = "FAILED"


class Artifact(Entity):
    artifact_id: str
    user_id: str
    chat_id: str
    kind: ArtifactKind
    name: str
    media_type: str
    size_bytes: int
    # Private S3 key; never a public URL.
    storage_ref: str
    status: ArtifactStatus
    index_status: IndexStatus
    # Set on a dataset produced by ingestion from an upload.
    derived_from_artifact_id: str | None = None
    created_at: str
