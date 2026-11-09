"""Chat and request rules behind the HTTP routes: users, chats, submit, cancel, replies, the event
stream's view of a request and paging. Routes stay thin; every rule here runs without HTTP."""

import base64
import json
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError

from contextforge.core.activity import ActivityBus
from contextforge.core.entities import wire
from contextforge.core.entities.artifact import Artifact, ArtifactKind, ArtifactStatus
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
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.user import User
from contextforge.core.ids import new_ulid, now_iso
from contextforge.core.models.transcript import Transcript
from contextforge.core.queue import Queue, WorkMessage
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.repositories.memory import Repositories
from contextforge.core.services.memory import (
    MemoryEditRefused,
    checked_instructions,
    merged_preferences,
)
from contextforge.core.services.submit import submit_request
from contextforge.core.services.trace import TraceRecords, build_trace
from contextforge.core.storage import BlobStore

TERMINAL = {RequestStatus.COMPLETED, RequestStatus.FAILED, RequestStatus.CANCELLED}
# Kinds whose stored bytes the API serves: a long report, and the web snapshots it cites.
READABLE_KINDS = {ArtifactKind.DELIVERABLE, ArtifactKind.RAW_RESULT}
TITLE_CHARS = 60
NOT_PENDING = "This request is not waiting for an answer to that message."

logger = logging.getLogger(__name__)


class ServiceDeps(Protocol):
    """What the application needs from the worker's dependency bundle. A Protocol, so ``core``
    imports nothing from ``worker`` (spec 12); ``WorkerDependencies`` satisfies it."""

    repositories: Repositories
    blobs: BlobStore
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
    def __init__(self, worker_dependencies: ServiceDeps, user_id: str) -> None:
        self.worker_dependencies, self.user_id = worker_dependencies, user_id
        # The API's own status moves, cancel and reply, run one at a time, so a cancel cannot
        # land between a reply's claim and the save of its answer.
        self.reply_lock = threading.Lock()

    def me(self) -> User:
        user = self.worker_dependencies.repositories.users.get(self.user_id)
        if user is None:
            now = now_iso()
            user = User(
                user_id=self.user_id,
                email="local@example.invalid",
                status="ACTIVE",
                created_at=now,
                updated_at=now,
            )
            self.worker_dependencies.repositories.users.save(user)
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
        self.worker_dependencies.repositories.users.save(updated)
        return updated

    def chats(self) -> list[Chat]:
        """The user's chats, most recently updated first; archived chats are hidden (E16)."""
        return [
            chat
            for chat in self.worker_dependencies.repositories.chats.list_by_user(self.user_id)
            if chat.status == ChatStatus.ACTIVE
        ]

    def chat(self, chat_id: str) -> Chat:
        chat = self.worker_dependencies.repositories.chats.get(self.user_id, chat_id)
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
        self.worker_dependencies.repositories.chats.save(chat)
        return chat

    def update_chat(self, chat_id: str, *, title: str | None = None, archive: bool = False) -> Chat:
        chat = self.chat(chat_id)
        if archive and any(
            isinstance(m, RequestMessage) and m.status not in TERMINAL
            for m in self.worker_dependencies.repositories.messages.list_by_chat(chat_id)
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
        self.worker_dependencies.repositories.chats.save(updated)
        return updated

    def messages(self, chat_id: str) -> list[Message]:
        self.chat(chat_id)
        return sorted(
            self.worker_dependencies.repositories.messages.list_by_chat(chat_id),
            key=lambda m: (m.created_at, m.message_id),
        )

    def artifacts(self, chat_id: str) -> list[Artifact]:
        self.chat(chat_id)
        return self.worker_dependencies.repositories.artifacts.list_by_chat(chat_id)

    def artifact_content(self, chat_id: str, artifact_id: str) -> tuple[Artifact, bytes]:
        """The Artifact and its stored bytes; 404 unless it is an AVAILABLE deliverable or
        raw_result of this user's chat whose bytes are still stored (a raw result expires)."""
        self.chat(chat_id)
        artifact = self.worker_dependencies.repositories.artifacts.get(chat_id, artifact_id)
        if (
            artifact is None
            or artifact.user_id != self.user_id
            or artifact.kind not in READABLE_KINDS
            or artifact.status != ArtifactStatus.AVAILABLE
        ):
            raise ServiceError(404, "not_found", "Artifact not found.")
        try:
            return artifact, self.worker_dependencies.blobs.get(artifact.storage_ref)
        except LookupError as gone:
            raise ServiceError(404, "not_found", "Artifact content is no longer stored.") from gone

    def request(self, chat_id: str, message_id: str) -> RequestMessage:
        self.chat(chat_id)
        message = self.worker_dependencies.repositories.messages.get(chat_id, message_id)
        if not isinstance(message, RequestMessage) or message.user_id != self.user_id:
            raise ServiceError(404, "not_found", "Request not found.")
        return message

    def submit(self, chat_id: str, content: str, key: str) -> RequestMessage:
        if not content.strip():
            raise ServiceError(400, "validation", "Question cannot be blank.")
        chat = self.chat(chat_id)
        message = submit_request(
            self.worker_dependencies.repositories,
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
        self.worker_dependencies.queue.send(
            WorkMessage(kind="start", chat_id=chat_id, message_id=message.message_id)
        )
        self.worker_dependencies.touch_chat(chat_id, self.user_id)
        return message

    def detail(self, chat_id: str, message_id: str) -> dict[str, Any]:
        request = self.request(chat_id, message_id)
        calls = self.worker_dependencies.repositories.model_calls.list_by_request(message_id)
        return {
            "request": wire(request),
            "tasks": [
                wire(t)
                for t in self.worker_dependencies.repositories.tasks.list_by_request(message_id)
            ],
            "usage": self._usage(message_id, calls),
        }

    def _usage(self, message_id: str, calls: list[ModelCall]) -> dict[str, Any]:
        """The request's summed usage and estimated cost over its recorded ``calls``."""
        usage = self.worker_dependencies.repositories.model_calls.sum_usage(message_id)
        return {
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
        }

    def trace(self, chat_id: str, message_id: str) -> dict[str, Any]:
        """The Deep Dive of one request (``core.services.trace``): what the planner was asked
        and answered, the tasks it created, each task's model and tool calls, and the report.

        Only task calls' transcripts are read, to derive their tool calls; the client fetches
        a transcript itself when it shows one.
        """
        request = self.request(chat_id, message_id)
        calls = sorted(
            self.worker_dependencies.repositories.model_calls.list_by_request(message_id),
            key=lambda c: (c.started_at, c.model_call_id),
        )
        transcripts = {
            c.model_call_id: record
            for c in calls
            if c.task_id is not None and (record := self._stored_transcript(c)) is not None
        }
        entries = [
            m
            for m in self.worker_dependencies.repositories.messages.list_by_chat(chat_id)
            if getattr(m, "request_message_id", None) == message_id
        ]
        return build_trace(
            TraceRecords(
                request=request,
                usage=self._usage(message_id, calls),
                tasks=self.worker_dependencies.repositories.tasks.list_by_request(message_id),
                calls=calls,
                entries=entries,
                transcripts=transcripts,
            )
        )

    def transcript(self, chat_id: str, message_id: str, model_call_id: str) -> bytes:
        """The stored transcript JSON of one ModelCall of this user's request; 404 when the
        call is not the request's, recorded none, or its blob is gone."""
        self.request(chat_id, message_id)
        call = self.worker_dependencies.repositories.model_calls.get(message_id, model_call_id)
        if call is None or call.transcript_ref is None:
            raise ServiceError(404, "not_found", "No transcript was recorded for that call.")
        try:
            return self.worker_dependencies.blobs.get(call.transcript_ref)
        except LookupError as gone:
            raise ServiceError(404, "not_found", "That transcript is no longer stored.") from gone

    def _stored_transcript(self, call: ModelCall) -> Transcript | None:
        """The call's transcript, or None when none was recorded or it cannot be read; the
        trace then shows the call without its tool calls."""
        if call.transcript_ref is None:
            return None
        try:
            record: Transcript = json.loads(self.worker_dependencies.blobs.get(call.transcript_ref))
        except (LookupError, ValueError):
            logger.warning(
                "transcript of model call %s could not be read", call.model_call_id, exc_info=True
            )
            return None
        return record

    def request_view(self, chat_id: str, message_id: str) -> RequestView:
        """The request, its Tasks and its assistant Messages as the event stream shows them.

        Read in this order so ``closed`` never runs ahead of the records: the status, then the
        pending question, then the Messages, which therefore include that question. The worker
        writes WAITING_USER before the question, so the status alone cannot close the stream.
        """
        request = self.request(chat_id, message_id)
        tasks = self.worker_dependencies.repositories.tasks.list_by_request(message_id)
        closed = request.status in TERMINAL or (
            request.status == RequestStatus.WAITING_USER
            and bool(
                self.worker_dependencies.repositories.messages.pending_for_request(
                    chat_id, message_id
                )
            )
        )
        messages = [
            m
            for m in self.worker_dependencies.repositories.messages.list_by_chat(chat_id)
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
        return self.worker_dependencies.activity.since(message_id, after)

    def cancel(self, chat_id: str, message_id: str) -> RequestMessage:
        with self.reply_lock:
            request = self.request(chat_id, message_id)
            if request.status in TERMINAL:
                raise ServiceError(409, "invalid_state", f"Request is {request.status}.")
            try:
                updated = self.worker_dependencies.repositories.messages.update_request_status(
                    chat_id, message_id, request.status, RequestStatus.CANCELLED
                )
            except StaleStatusError as stale:  # the worker moved it first
                raise ServiceError(409, "invalid_state", f"Request is {stale.actual}.") from stale
        self.worker_dependencies.touch_chat(chat_id, self.user_id)
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
            pending = self.worker_dependencies.repositories.messages.pending_for_request(
                chat_id, message_id
            )
            if not isinstance(question, ClarificationMessage) or question not in pending:
                raise ServiceError(409, "not_pending", NOT_PENDING)
            self._claim(chat_id, message_id)
            reply = self._save_answer(question, content)
        self.worker_dependencies.touch_chat(chat_id, self.user_id)
        self.worker_dependencies.queue.send(
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
        entry = self.worker_dependencies.repositories.messages.get(chat_id, entry_id)
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
            self.worker_dependencies.repositories.messages.update_request_status(
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
            self.worker_dependencies.repositories.messages.save(reply)
        except Exception:
            # Hand the question back rather than leave the request RUNNING with no answer and
            # no resume queued; the user can answer again.
            self.worker_dependencies.repositories.messages.update_request_status(
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
