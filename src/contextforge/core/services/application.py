"""Chat/request application rules shared by HTTP handlers and the queue worker."""

import base64
import json
import threading
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from contextforge.core.entities.chat import Chat, ChatStatus
from contextforge.core.entities.message import (
    ClarificationResponseMessage,
    Message,
    RequestMessage,
    RequestStatus,
)
from contextforge.core.entities.user import User
from contextforge.core.ids import new_ulid, now_iso
from contextforge.core.queue import WorkMessage
from contextforge.core.services.submit import submit_request
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.runner import RequestRunner

TERMINAL = {RequestStatus.COMPLETED, RequestStatus.FAILED, RequestStatus.CANCELLED}


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


class Application:
    def __init__(self, deps: WorkerDeps, runner: RequestRunner, user_id: str) -> None:
        self.deps, self.runner, self.user_id = deps, runner, user_id
        self.reply_lock = threading.Lock()

    def me(self) -> User:
        user = self.deps.repos.users.get(self.user_id)
        if user is None:
            now = now_iso()
            user = User(
                user_id=self.user_id,
                email="local@example.invalid",
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
            self.deps.repos.users.save(user)
        return user

    def update_me(self, values: dict[str, Any]) -> User:
        user = self.me()
        if "preferences" in values and values["preferences"] is not None:
            preferences = {**(user.preferences or {}), **values["preferences"]}
            if set(preferences) - {"currency", "locale", "timezone"}:
                raise ServiceError(400, "validation", "Unknown preference.")
            try:
                ZoneInfo(preferences.get("timezone", "Asia/Kolkata"))
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ServiceError(400, "validation", "Unknown timezone.") from exc
            values["preferences"] = preferences
        user = User.model_validate({**user.model_dump(), **values, "updated_at": now_iso()})
        self.deps.repos.users.save(user)
        return user

    def chat(self, chat_id: str) -> Chat:
        chat = self.deps.repos.chats.get(self.user_id, chat_id)
        if chat is None or chat.status != ChatStatus.ACTIVE:
            raise ServiceError(404, "not_found", "Chat not found.")
        return chat

    def create_chat(self) -> Chat:
        now = now_iso()
        chat = Chat(
            chat_id=new_ulid(),
            user_id=self.user_id,
            title="",
            status="ACTIVE",
            created_at=now,
            updated_at=now,
        )
        self.deps.repos.chats.save(chat)
        return chat

    def update_chat(self, chat_id: str, *, title: str | None = None, archive: bool = False) -> Chat:
        chat = self.chat(chat_id)
        if archive and any(
            isinstance(m, RequestMessage) and m.status not in TERMINAL
            for m in self.deps.repos.messages.list_by_chat(chat_id)
        ):
            raise ServiceError(
                409, "invalid_state", "Cancel active requests before archiving this Chat."
            )
        updated = chat.model_copy(
            update={
                "title": title if title is not None else chat.title,
                "status": ChatStatus.ARCHIVED if archive else chat.status,
                "updated_at": now_iso(),
            }
        )
        self.deps.repos.chats.save(updated)
        return updated

    def messages(self, chat_id: str) -> list[Message]:
        self.chat(chat_id)
        return sorted(
            self.deps.repos.messages.list_by_chat(chat_id),
            key=lambda m: (m.created_at, m.message_id),
        )

    def request(self, chat_id: str, message_id: str) -> RequestMessage:
        self.chat(chat_id)
        message = self.deps.repos.messages.get(chat_id, message_id)
        if not isinstance(message, RequestMessage) or message.user_id != self.user_id:
            raise ServiceError(404, "not_found", "Request not found.")
        return message

    def submit(self, chat_id: str, content: str, key: str) -> RequestMessage:
        chat = self.chat(chat_id)
        message = submit_request(
            self.deps.repos,
            client_key=f"{self.user_id}:{chat_id}:{key}",
            chat_id=chat_id,
            user_id=self.user_id,
            content=content,
        )
        if message.content != content:
            raise ServiceError(
                409, "invalid_state", "This idempotency key was used for another request."
            )
        if not chat.title:
            self.update_chat(chat_id, title=" ".join(content.split())[:60])
        self.deps.queue.send(
            WorkMessage(kind="start", chat_id=chat_id, message_id=message.message_id)
        )
        self.deps.touch_chat(chat_id, self.user_id)
        return message

    def detail(self, chat_id: str, message_id: str) -> dict[str, Any]:
        request = self.request(chat_id, message_id)
        calls = self.deps.repos.model_calls.list_by_request(message_id)
        usage = self.deps.repos.model_calls.sum_usage(message_id)
        return {
            "request": wire(request),
            "tasks": [wire(t) for t in self.deps.repos.tasks.list_by_request(message_id)],
            "usage": {
                **(wire(usage) if usage else {}),
                "modelCalls": len(calls),
                "estimatedCostUsd": sum(c.estimated_cost_usd or 0 for c in calls),
                "usageComplete": all(c.usage is not None for c in calls),
                "costBasis": "conservative per-call reservation; not verified billing",
            },
        }

    def cancel(self, chat_id: str, message_id: str) -> RequestMessage:
        request = self.request(chat_id, message_id)
        if request.status in TERMINAL:
            raise ServiceError(409, "invalid_state", f"Request is {request.status}.")
        updated = self.deps.repos.messages.update_request_status(
            chat_id, message_id, request.status, RequestStatus.CANCELLED
        )
        self.deps.touch_chat(chat_id, self.user_id)
        return updated

    def reply(self, chat_id: str, message_id: str, target: str, content: str) -> Message:
        with self.reply_lock:
            request = self.request(chat_id, message_id)
            pending = self.runner.pending_interrupt(message_id)
            if (
                request.status != RequestStatus.WAITING_USER
                or pending is None
                or pending["message_id"] != target
            ):
                raise ServiceError(
                    409, "not_pending", "Reply does not target the pending clarification."
                )
            question = self.deps.repos.messages.get(chat_id, target)
            if (
                question is None
                or question.kind != "clarification"
                or question.user_id != self.user_id
                or getattr(question, "request_message_id", None) != message_id
            ):
                raise ServiceError(404, "not_found", "Clarification not found.")
            if any(
                m.in_reply_to_message_id == target
                for m in self.messages(chat_id)
                if isinstance(m, ClarificationResponseMessage)
            ):
                raise ServiceError(409, "not_pending", "This clarification already has a reply.")
            now = now_iso()
            reply = ClarificationResponseMessage(
                message_id=new_ulid(),
                chat_id=chat_id,
                user_id=self.user_id,
                sender="user",
                kind="clarification_response",
                content=content,
                request_message_id=message_id,
                in_reply_to_message_id=target,
                created_at=now,
                updated_at=now,
            )
            self.deps.repos.messages.save(reply)
            self.deps.repos.messages.update_request_status(
                chat_id, message_id, RequestStatus.WAITING_USER, RequestStatus.RUNNING
            )
            self.deps.touch_chat(chat_id, self.user_id)
            self.deps.queue.send(
                WorkMessage(
                    kind="resume",
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_message_id=reply.message_id,
                )
            )
            return reply


def wire(value: Any) -> dict[str, Any]:
    return dict(value.model_dump(mode="json", by_alias=True))


def page(
    items: list[Any], scope: str, limit: int, cursor: str | None, *, newest: bool = False
) -> dict[str, Any]:
    offset = 0
    if cursor:
        try:
            data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if data["scope"] != scope or type(data["offset"]) is not int or data["offset"] < 0:
                raise ValueError()
            offset = data["offset"]
        except (ValueError, KeyError, TypeError) as exc:
            raise ServiceError(400, "validation", "Invalid paging cursor.") from exc
    ordered = list(reversed(items)) if newest else items
    selected = ordered[offset : offset + limit]
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps({"scope": scope, "offset": offset + limit}).encode()
        ).decode()
        if offset + limit < len(items)
        else None
    )
    if newest:
        selected.reverse()
    return {"items": [wire(x) for x in selected], "nextCursor": next_cursor}
