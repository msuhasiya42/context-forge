"""Files a chat's research produced: the list and each one's content."""

import re
from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from contextforge.api.deps import ServiceDep, authorized
from contextforge.core.services.application import page

router = APIRouter(
    prefix="/chats/{chat_id}/artifacts", tags=["artifacts"], dependencies=[Depends(authorized)]
)


@router.get("")
def artifacts(
    chat_id: str,
    service: ServiceDep,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, Any]:
    return page(service.artifacts(chat_id), f"artifacts:{chat_id}", limit, cursor)


@router.get("/{artifact_id}/content")
def artifact_content(chat_id: str, artifact_id: str, service: ServiceDep) -> Response:
    artifact, data = service.artifact_content(chat_id, artifact_id)
    # attachment and nosniff: a snapshot holds page text, which must never be rendered as a page
    # of this origin if the URL is opened directly.
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", artifact.name)
    return Response(
        data,
        media_type=artifact.media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
