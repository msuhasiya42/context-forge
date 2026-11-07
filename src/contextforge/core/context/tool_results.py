"""Tool Result Processor (spec 8.3): cap the rows a model sees, offload the rest to an Artifact."""

import json
from dataclasses import dataclass
from itertools import islice
from typing import Any

from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus, IndexStatus
from contextforge.core.ids import derived_id, now_iso
from contextforge.core.repositories.base import ArtifactRepository
from contextforge.core.storage import BlobStore, raw_result_key


@dataclass(frozen=True)
class ProcessedRows:
    """What the model sees of one tool result.

    ``rows`` are the kept, shaped rows; ``artifact_id`` names the raw_result Artifact holding
    every row when some were left out, and is None otherwise.
    """

    rows: list[dict[str, Any]]
    total_rows: int
    artifact_id: str | None
    text: str


def raw_result_id(task_id: str, tool_call_id: str) -> str:
    """The raw_result Artifact id for one tool call, so a replayed call overwrites its Artifact."""
    return derived_id(f"{task_id}:{tool_call_id}")


def process_rows(
    rows: list[dict[str, Any]],
    *,
    cap: int,
    artifacts: ArtifactRepository,
    blobs: BlobStore,
    chat_id: str,
    user_id: str,
    name: str,
    artifact_id: str,
    column_cap: int = 20,
    cell_char_cap: int = 200,
) -> ProcessedRows:
    """Keep at most ``cap`` rows, shaped to ``column_cap`` columns and ``cell_char_cap`` chars.

    When rows are left out or shaping cut a cell or column, every row (unshaped) is written to a
    raw_result Artifact under ``artifact_id``, so nothing the model sees as cut is lost. The
    caller derives that id with ``raw_result_id`` so the write is idempotent. Zero rows is a
    valid result, not an error.
    """
    kept = [_shape_row(row, column_cap, cell_char_cap) for row in rows[:cap]]
    offloaded = len(rows) > cap or _was_shaped(rows[:cap], kept)
    if offloaded:
        _save_raw_result(
            rows,
            artifacts,
            blobs,
            chat_id=chat_id,
            user_id=user_id,
            name=name,
            artifact_id=artifact_id,
        )
    stored_id = artifact_id if offloaded else None
    return ProcessedRows(
        rows=kept,
        total_rows=len(rows),
        artifact_id=stored_id,
        text=_result_text(len(kept), len(rows), stored_id),
    )


def _was_shaped(original: list[dict[str, Any]], shaped: list[dict[str, Any]]) -> bool:
    """True when shaping cut a column or a cell, so the kept rows are not the full rows.

    Shaping only drops keys or replaces long strings, so any difference is a cut; unchanged
    cells are the same objects, so the comparison is by identity for them.
    """
    return shaped != original


def _result_text(shown: int, total: int, artifact_id: str | None) -> str:
    """The stub the model reads; it always starts "showing N of M rows"."""
    head = f"showing {shown} of {total} rows"
    if artifact_id is None:
        return head
    if shown < total:
        return f"{head}; {total - shown} more row(s) in artifact {artifact_id}"
    return f"{head}; full rows in artifact {artifact_id}"


def _shape_row(row: dict[str, Any], column_cap: int, cell_char_cap: int) -> dict[str, Any]:
    """The row's first ``column_cap`` columns, each string cell cut to ``cell_char_cap``."""
    return {
        key: _shape_cell(value, cell_char_cap) for key, value in islice(row.items(), column_cap)
    }


def _shape_cell(value: Any, cell_char_cap: int) -> Any:
    if not isinstance(value, str) or len(value) <= cell_char_cap:
        return value
    return f"{value[:cell_char_cap]}…[+{len(value) - cell_char_cap} chars]"


def _save_raw_result(
    rows: list[dict[str, Any]],
    artifacts: ArtifactRepository,
    blobs: BlobStore,
    *,
    chat_id: str,
    user_id: str,
    name: str,
    artifact_id: str,
) -> None:
    # One sorted-key JSON object per line, newline-terminated: replaying the same rows writes
    # the same bytes.
    data = "".join(f"{json.dumps(row, sort_keys=True)}\n" for row in rows).encode("utf-8")
    storage_ref = blobs.put(raw_result_key(chat_id, artifact_id), data)
    artifacts.save(
        Artifact(
            artifact_id=artifact_id,
            user_id=user_id,
            chat_id=chat_id,
            kind=ArtifactKind.RAW_RESULT,
            name=name,
            media_type="application/x-ndjson",
            size_bytes=len(data),
            storage_ref=storage_ref,
            status=ArtifactStatus.AVAILABLE,
            index_status=IndexStatus.NOT_INDEXED,
            created_at=now_iso(),
        )
    )
