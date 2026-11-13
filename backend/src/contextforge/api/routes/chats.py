"""Chats and their message history."""

from typing import Any

from fastapi import APIRouter, Depends, Query, Response

from contextforge.api.deps import ServiceDep, authorized
from contextforge.api.schemas import NewChatBody, RenameBody
from contextforge.core.entities import wire
from contextforge.core.services.application import page

router = APIRouter(prefix="/chats", tags=["chats"], dependencies=[Depends(authorized)])


@router.post("", status_code=201)
def new_chat(service: ServiceDep, body: NewChatBody | None = None) -> dict[str, Any]:
    return wire(service.create_chat(body.title if body else None))


@router.get("")
def list_chats(
    service: ServiceDep, limit: int = Query(50, ge=1, le=100), cursor: str | None = None
) -> dict[str, Any]:
    return page(service.chats(), f"chats:{service.user_id}", limit, cursor)


@router.patch("/{chat_id}")
def rename_chat(chat_id: str, body: RenameBody, service: ServiceDep) -> dict[str, Any]:
    return wire(service.update_chat(chat_id, title=body.title))


@router.delete("/{chat_id}", status_code=204)
def delete_chat(chat_id: str, service: ServiceDep) -> Response:
    service.update_chat(chat_id, archive=True)
    return Response(status_code=204)


@router.get("/{chat_id}/messages")
def messages(
    chat_id: str,
    service: ServiceDep,
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = None,
) -> dict[str, Any]:
    return page(service.messages(chat_id), f"messages:{chat_id}", limit, cursor, newest=True)
