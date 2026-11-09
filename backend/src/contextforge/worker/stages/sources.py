"""The ``<sources>`` a stage shows the model: the chat's AVAILABLE Artifacts, one line each."""

import csv
import io

from contextforge.core.context.builder import SourceLine
from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus
from contextforge.core.repositories.base import ArtifactRepository
from contextforge.core.storage import BlobStore


def available_artifacts(artifacts: ArtifactRepository, chat_id: str) -> list[Artifact]:
    """The chat's Artifacts a task may read, in ``artifact_id`` order."""
    return [a for a in artifacts.list_by_chat(chat_id) if a.status == ArtifactStatus.AVAILABLE]


def source_line(artifact: Artifact, blobs: BlobStore) -> SourceLine:
    """A dataset's line carries its row count and columns; other kinds their media type."""
    if artifact.kind == ArtifactKind.DATASET:
        detail = _dataset_detail(blobs.get(artifact.storage_ref))
    else:
        detail = artifact.media_type
    return SourceLine(artifact.artifact_id, artifact.kind.value, artifact.name, detail)


def _dataset_detail(csv_bytes: bytes) -> str:
    """``rows: <n>, columns: <a>, <b>, ...`` from a CSV with a header row; blank lines skipped."""
    # utf-8-sig: spreadsheet exports often start with a byte-order mark.
    records = [r for r in csv.reader(io.StringIO(csv_bytes.decode("utf-8-sig"))) if r]
    if not records:
        return "rows: 0, columns: (none)"
    header, *rows = records
    return f"rows: {len(rows)}, columns: {', '.join(header)}"
