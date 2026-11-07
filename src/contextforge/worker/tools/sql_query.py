"""``sql_query`` (spec 9.2): one read-only SELECT over a chat's dataset, exposed as table ``t``."""

import tempfile
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus
from contextforge.core.models.base import ToolSpec
from contextforge.core.repositories.base import ArtifactRepository
from contextforge.core.storage import BlobStore
from contextforge.core.tools.base import ToolContext, ToolPermission, ToolResult

_DESCRIPTION = (
    "Run a read-only SQL query over one typed dataset using DuckDB. The dataset is exposed as a "
    "table named `t`. Returns at most 50 rows; if more match, the full result is stored as an "
    "artifact and the response says how many rows were omitted. Use aggregates and filters to "
    "stay under the limit. Monetary amounts are numeric; never compute totals yourself from "
    "returned rows."
)
_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset_id": {
            "type": "string",
            "description": "Artifact id of the dataset, from the task inputs",
        },
        "sql": {"type": "string", "description": "A single SELECT statement against table t"},
    },
    "required": ["dataset_id", "sql"],
    "additionalProperties": False,
}
_SELECT_KEYWORDS = ("SELECT", "WITH")


class SqlQueryTool:
    """Loads the dataset into a fresh in-memory DuckDB per call; nothing is shared between calls.

    The SELECT check is a convenience guard, not a security boundary: the database is a
    throwaway copy, so a statement that slipped through could change nothing that lasts.
    """

    spec: ToolSpec = ToolSpec(
        name="sql_query", description=_DESCRIPTION, input_schema=_INPUT_SCHEMA
    )
    permission: ToolPermission = "READ"

    def __init__(self, artifacts: ArtifactRepository, blobs: BlobStore) -> None:
        self._artifacts = artifacts
        self._blobs = blobs

    def run(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Rows of the query; bad arguments, an unknown dataset or bad SQL are ``code`` errors."""
        dataset_id, sql = arguments.get("dataset_id"), arguments.get("sql")
        if not isinstance(dataset_id, str) or not isinstance(sql, str):
            return _code_error("sql_query needs the string arguments dataset_id and sql")
        artifact = self._artifacts.get(ctx.chat_id, dataset_id)
        if artifact is None or not _is_available_dataset(artifact):
            return _code_error(f"no available dataset {dataset_id} in this chat")
        rejection = _select_rejection(sql)
        if rejection is not None:
            return _code_error(rejection)
        try:
            rows = _select_rows(self._blobs.get(artifact.storage_ref), sql.strip())
        except duckdb.Error as exc:
            return _code_error(str(exc))
        return ToolResult(rows=rows)


def _is_available_dataset(artifact: Artifact) -> bool:
    return artifact.kind == ArtifactKind.DATASET and artifact.status == ArtifactStatus.AVAILABLE


def _select_rejection(sql: str) -> str | None:
    """Why ``sql`` is not a single SELECT, or None; DuckDB's EXPLAIN catches the rest."""
    words = sql.strip().split(maxsplit=1)
    if not words or words[0].upper() not in _SELECT_KEYWORDS:
        return "only a single SELECT (or WITH ... SELECT) statement is allowed"
    if ";" in sql:
        return "only a single statement is allowed; remove the ';'"
    return None


def _select_rows(csv_bytes: bytes, sql: str) -> list[dict[str, Any]]:
    """Run ``sql`` over the CSV loaded as table ``t``; values come back JSON-safe."""
    with tempfile.TemporaryDirectory() as tmp, duckdb.connect(":memory:") as con:
        path = Path(tmp) / "dataset.csv"
        path.write_bytes(csv_bytes)
        con.execute("CREATE TABLE t AS SELECT * FROM read_csv_auto(?)", [str(path)])
        # Parse and bind without running, so a malformed statement fails before any work.
        con.execute(f"EXPLAIN {sql}")
        cursor = con.execute(sql)
        columns = [column[0] for column in cursor.description or []]
        return [
            {name: _json_value(value) for name, value in zip(columns, row, strict=True)}
            for row in cursor.fetchall()
        ]


def _json_value(value: Any) -> Any:
    """A DuckDB value as a JSON value: rows are written as JSON lines and shown as JSON."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, date | time):  # datetime is a date
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    # UUID, timedelta and other scalars have no JSON form; their text is what the model reads.
    return str(value)


def _code_error(message: str) -> ToolResult:
    """A failure the model caused and can fix by changing its arguments."""
    return ToolResult(status="error", error=message, error_class="code", retryable=False)
