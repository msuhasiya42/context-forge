"""Chat and request rules behind the HTTP routes: users, chats, submit, cancel, replies, the event
stream's view of a request and paging. Routes stay thin; every rule here runs without HTTP."""

import base64
import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from contextforge.core.activity import ActivityBus
from contextforge.core.entities.artifact import Artifact
from contextforge.core.entities.chat import Chat, ChatStatus
from contextforge.core.entities.message import (
    ClarificationMessage,
    ClarificationResponseMessage,
    Message,
    MessageKind,
    RequestMessage,
    RequestStatus,
    Sender,
)
from contextforge.core.entities.user import User
from contextforge.core.ids import new_ulid, now_iso
from contextforge.core.queue import Queue, WorkMessage
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.repositories.memory import Repositories
from contextforge.core.services.memory import (
    MemoryEditRefused,
    checked_instructions,
    merged_preferences,
)
from contextforge.core.services.submit import submit_request

TERMINAL = {RequestStatus.COMPLETED, RequestStatus.FAILED, RequestStatus.CANCELLED}
TITLE_CHARS = 60
NOT_PENDING = "This request is not waiting for an answer to that message."


class ServiceDeps(Protocol):
    """What the application needs from the worker's dependency bundle. A Protocol, so ``core``
    imports nothing from ``worker`` (spec 12); ``WorkerDeps`` satisfies it."""

    repos: Repositories
    queue: Queue
    activity: ActivityBus

    def touch_chat(self, chat_id: str, user_id: str) -> None: ...


class ServiceError(Exception):
    """An expected failure the API returns as ``{"error": {"code", "message"}}``."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status, self.code, self.message = status, code, message


@dataclass(frozen=True)
class RequestView:
    """One poll of a request for the event stream (spec 11.5), as wire records. ``closed`` is
    True once the request ended, or waits on the user with its question already visible."""

    request: dict[str, Any]
    tasks: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    closed: bool


class Application:
    def __init__(self, deps: ServiceDeps, user_id: str) -> None:
        self.deps, self.user_id = deps, user_id
        # The API's own status moves, cancel and reply, run one at a time, so a cancel cannot
        # land between a reply's claim and the save of its answer.
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

    def update_me(self, changes: Mapping[str, Any]) -> User:
        """Apply a PATCH /me body; ``changes`` holds only the fields the client sent.

        Preferences merge key by key and None leaves them as they are; instructions replace the
        stored ones and None or blank clears them. A credential in the instructions is refused.
        """
        user = self.me()
        update: dict[str, Any] = {"updated_at": now_iso()}
        try:
            if changes.get("preferences") is not None:
                update["preferences"] = merged_preferences(user.preferences, changes["preferences"])
            if "instructions" in changes:
                update["instructions"] = checked_instructions(changes["instructions"])
            updated = User.model_validate({**user.model_dump(), **update})
        except MemoryEditRefused as refused:
            raise ServiceError(400, refused.code, refused.message) from refused
        except ValidationError as invalid:
            raise ServiceError(
                400, "validation", "Invalid preferences or instructions."
            ) from invalid
        self.deps.repos.users.save(updated)
        return updated

    def chats(self) -> list[Chat]:
        """The user's chats, most recently updated first; archived chats are hidden (E16)."""
        return [
            chat
            for chat in self.deps.repos.chats.list_by_user(self.user_id)
            if chat.status == ChatStatus.ACTIVE
        ]

    def chat(self, chat_id: str) -> Chat:
        chat = self.deps.repos.chats.get(self.user_id, chat_id)
        if chat is None or chat.status != ChatStatus.ACTIVE:
            raise ServiceError(404, "not_found", "Chat not found.")
        return chat

    def create_chat(self, title: str | None = None) -> Chat:
        """A new chat; without ``title`` the first request names it."""
        now = now_iso()
        chat = Chat(
            chat_id=new_ulid(),
            user_id=self.user_id,
            title=title or "",
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

    def artifacts(self, chat_id: str) -> list[Artifact]:
        self.chat(chat_id)
        return self.deps.repos.artifacts.list_by_chat(chat_id)

    def request(self, chat_id: str, message_id: str) -> RequestMessage:
        self.chat(chat_id)
        message = self.deps.repos.messages.get(chat_id, message_id)
        if not isinstance(message, RequestMessage) or message.user_id != self.user_id:
            raise ServiceError(404, "not_found", "Request not found.")
        return message

    def submit(self, chat_id: str, content: str, key: str) -> RequestMessage:
        if not content.strip():
            raise ServiceError(400, "validation", "Question cannot be blank.")
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
            self.update_chat(chat_id, title=title_from(content))
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
                "costBasis": (
                    "each finished call priced from its token usage at config/models.yaml "
                    "prices; a call with unknown usage counts at the per-call reserve; calls "
                    "in flight are not shown (the spend cap holds the reserve for them); not "
                    "verified billing"
                ),
            },
        }

    def request_view(self, chat_id: str, message_id: str) -> RequestView:
        """The request, its Tasks and its assistant Messages as the event stream shows them.

        Read in this order so ``closed`` never runs ahead of the records: the status, then the
        pending question, then the Messages, which therefore include that question. The worker
        writes WAITING_USER before the question, so the status alone cannot close the stream.
        """
        request = self.request(chat_id, message_id)
        tasks = self.deps.repos.tasks.list_by_request(message_id)
        closed = request.status in TERMINAL or (
            request.status == RequestStatus.WAITING_USER
            and bool(self.deps.repos.messages.pending_for_request(chat_id, message_id))
        )
        messages = [
            m
            for m in self.deps.repos.messages.list_by_chat(chat_id)
            if m.sender != Sender.USER and getattr(m, "request_message_id", None) == message_id
        ]
        return RequestView(
            request=wire(request),
            tasks=[wire(t) for t in tasks],
            messages=[wire(m) for m in messages],
            closed=closed,
        )

    def activity_since(self, message_id: str, after: int) -> list[tuple[int, dict[str, str]]]:
        """Transient tool activity of the request after sequence ``after``; never persisted."""
        return self.deps.activity.since(message_id, after)

    def cancel(self, chat_id: str, message_id: str) -> RequestMessage:
        with self.reply_lock:
            request = self.request(chat_id, message_id)
            if request.status in TERMINAL:
                raise ServiceError(409, "invalid_state", f"Request is {request.status}.")
            try:
                updated = self.deps.repos.messages.update_request_status(
                    chat_id, message_id, request.status, RequestStatus.CANCELLED
                )
            except StaleStatusError as stale:  # the worker moved it first
                raise ServiceError(409, "invalid_state", f"Request is {stale.actual}.") from stale
        self.deps.touch_chat(chat_id, self.user_id)
        return updated

    def reply(
        self, chat_id: str, message_id: str, target: str, content: str
    ) -> ClarificationResponseMessage:
        """Answer the request's pending clarification and queue its resume (spec 11.4).

        404 when ``target`` is not an entry of this request; 409 ``not_pending`` unless the
        request is WAITING_USER on that exact question. The request is claimed WAITING_USER ->
        RUNNING before the answer is saved, so a lost race stores nothing, and a second answer
        to the same question is a 409.
        """
        if not content.strip():
            raise ServiceError(400, "validation", "Answer cannot be blank.")
        with self.reply_lock:
            self.request(chat_id, message_id)
            question = self._entry(chat_id, message_id, target)
            pending = self.deps.repos.messages.pending_for_request(chat_id, message_id)
            if not isinstance(question, ClarificationMessage) or question not in pending:
                raise ServiceError(409, "not_pending", NOT_PENDING)
            self._claim(chat_id, message_id)
            reply = self._save_answer(question, content)
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

    def _entry(self, chat_id: str, message_id: str, entry_id: str) -> Message:
        """Message ``entry_id``; 404 unless it is this user's and an entry of request
        ``message_id`` (the request Message itself is not one of its entries)."""
        entry = self.deps.repos.messages.get(chat_id, entry_id)
        if (
            entry is None
            or isinstance(entry, RequestMessage)
            or entry.request_message_id != message_id
            or entry.user_id != self.user_id
        ):
            raise ServiceError(404, "not_found", "That message is not part of this request.")
        return entry

    def _claim(self, chat_id: str, message_id: str) -> None:
        """WAITING_USER -> RUNNING; a request something else moved first is not pending."""
        try:
            self.deps.repos.messages.update_request_status(
                chat_id, message_id, RequestStatus.WAITING_USER, RequestStatus.RUNNING
            )
        except StaleStatusError as stale:
            raise ServiceError(409, "not_pending", NOT_PENDING) from stale

    def _save_answer(
        self, question: ClarificationMessage, content: str
    ) -> ClarificationResponseMessage:
        """Save the answer to ``question`` for a request this call has just claimed."""
        now = now_iso()
        reply = ClarificationResponseMessage(
            message_id=new_ulid(),
            chat_id=question.chat_id,
            user_id=self.user_id,
            sender=Sender.USER,
            kind=MessageKind.CLARIFICATION_RESPONSE,
            content=content,
            request_message_id=question.request_message_id,
            in_reply_to_message_id=question.message_id,
            created_at=now,
            updated_at=now,
        )
        try:
            self.deps.repos.messages.save(reply)
        except Exception:
            # Hand the question back rather than leave the request RUNNING with no answer and
            # no resume queued; the user can answer again.
            self.deps.repos.messages.update_request_status(
                question.chat_id,
                question.request_message_id,
                RequestStatus.RUNNING,
                RequestStatus.WAITING_USER,
            )
            raise
        return reply


def title_from(content: str) -> str:
    """A chat title from its first request: whitespace collapsed, and cut to ``TITLE_CHARS``
    with an ellipsis when longer."""
    text = " ".join(content.split())
    if len(text) <= TITLE_CHARS:
        return text
    return text[: TITLE_CHARS - 1].rstrip() + "…"


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
