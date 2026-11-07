"""Dict-backed repositories for tests and local runs: no network, no AWS.

The worker runs tasks on threads, so every repository serialises access with one lock. That
keeps listings safe from concurrent writes and makes each conditional update an atomic
compare-and-set, as DynamoDB's ``ConditionExpression`` is.

``model_copy`` skips validation, so status updates coerce ``new`` to its enum: callers outside
mypy (tests, decoded JSON) may pass the plain string, and the stored field must stay an enum.
"""

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from contextforge.core.entities.artifact import Artifact, ArtifactStatus
from contextforge.core.entities.chat import Chat
from contextforge.core.entities.message import (
    Message,
    RequestMessage,
    RequestStatus,
)
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.task import Task, TaskResult, TaskStatus
from contextforge.core.entities.user import User
from contextforge.core.ids import now_iso
from contextforge.core.repositories.base import (
    ArtifactRepository,
    ChatRepository,
    MessageRepository,
    ModelCallRepository,
    StaleStatusError,
    TaskRepository,
    UserRepository,
)
from contextforge.core.repositories.shared import sum_known_usage, unanswered
from contextforge.core.values import Usage


class InMemoryMessages:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._messages: dict[tuple[str, str], Message] = {}
        # Submission idempotency: client key -> (chat_id, message_id) of the request it created.
        self._client_keys: dict[str, tuple[str, str]] = {}

    def save(self, message: Message) -> None:
        with self._lock:
            self._messages[(message.chat_id, message.message_id)] = message

    def get(self, chat_id: str, message_id: str) -> Message | None:
        with self._lock:
            return self._messages.get((chat_id, message_id))

    def list_by_chat(self, chat_id: str) -> list[Message]:
        with self._lock:
            return self._in_chat(chat_id)

    def update_request_status(
        self,
        chat_id: str,
        message_id: str,
        expected: RequestStatus,
        new: RequestStatus,
        failure_reason: str | None = None,
    ) -> RequestMessage:
        with self._lock:
            current = self._request(chat_id, message_id)
            if current.status != expected:
                raise StaleStatusError(expected, current.status)
            changes: dict[str, Any] = {"status": RequestStatus(new), "updated_at": now_iso()}
            if failure_reason is not None:
                changes["failure_reason"] = failure_reason
            updated = current.model_copy(update=changes)
            self._messages[(chat_id, message_id)] = updated
            return updated

    def create_request(self, client_key: str, message: RequestMessage) -> RequestMessage:
        with self._lock:
            seen = self._client_keys.get(client_key)
            if seen is not None:
                return self._request(*seen)
            self._messages[(message.chat_id, message.message_id)] = message
            self._client_keys[client_key] = (message.chat_id, message.message_id)
            return message

    def pending_for_request(self, chat_id: str, request_message_id: str) -> list[Message]:
        with self._lock:
            in_chat = self._in_chat(chat_id)
        return unanswered(in_chat, request_message_id)

    def _in_chat(self, chat_id: str) -> list[Message]:
        """Caller holds the lock."""
        return sorted(
            (m for (c, _), m in self._messages.items() if c == chat_id),
            key=lambda m: m.message_id,
        )

    def _request(self, chat_id: str, message_id: str) -> RequestMessage:
        """Caller holds the lock."""
        message = self._messages.get((chat_id, message_id))
        if not isinstance(message, RequestMessage):
            raise LookupError(f"no request message {message_id} in chat {chat_id}")
        return message


class InMemoryTasks:
    def save_all(self, tasks: list[Task]) -> None:
        with self._lock:
            self._tasks.update({(t.request_message_id, t.task_id): t for t in tasks})
        for task in tasks:
            self._after_save(task)

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[tuple[str, str], Task] = {}
        # Test hook called after every stored write (save and update_status). It runs outside
        # the lock, so it may call back into the repositories; an exception it raises reaches
        # the caller after the write has landed, which is how tests simulate a crash.
        self.on_save: Callable[[Task], None] | None = None

    def save(self, task: Task) -> None:
        with self._lock:
            self._tasks[(task.request_message_id, task.task_id)] = task
        self._after_save(task)

    def get(self, request_message_id: str, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get((request_message_id, task_id))

    def list_by_request(self, request_message_id: str) -> list[Task]:
        with self._lock:
            return sorted(
                (t for (r, _), t in self._tasks.items() if r == request_message_id),
                key=lambda t: t.task_id,
            )

    def update_status(
        self,
        request_message_id: str,
        task_id: str,
        expected: TaskStatus,
        new: TaskStatus,
        *,
        result: TaskResult | None = None,
        failure_reason: str | None = None,
    ) -> Task:
        with self._lock:
            current = self._tasks.get((request_message_id, task_id))
            if current is None:
                raise LookupError(f"no task {task_id} in request {request_message_id}")
            if current.status != expected:
                raise StaleStatusError(expected, current.status)
            changes: dict[str, Any] = {"status": TaskStatus(new), "updated_at": now_iso()}
            if result is not None:
                changes["result"] = result
            if failure_reason is not None:
                changes["failure_reason"] = failure_reason
            updated = current.model_copy(update=changes)
            self._tasks[(request_message_id, task_id)] = updated
        self._after_save(updated)
        return updated

    def _after_save(self, task: Task) -> None:
        if self.on_save is not None:
            self.on_save(task)


class InMemoryModelCalls:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: dict[str, ModelCall] = {}

    def save(self, call: ModelCall) -> None:
        with self._lock:
            self._calls[call.model_call_id] = call

    def get(self, request_message_id: str, model_call_id: str) -> ModelCall | None:
        with self._lock:
            call = self._calls.get(model_call_id)
        return call if call is not None and call.request_message_id == request_message_id else None

    def list_by_request(self, request_message_id: str) -> list[ModelCall]:
        with self._lock:
            return sorted(
                (c for c in self._calls.values() if c.request_message_id == request_message_id),
                key=lambda c: c.model_call_id,
            )

    def sum_usage(self, request_message_id: str) -> Usage | None:
        return sum_known_usage(c.usage for c in self.list_by_request(request_message_id))


class InMemoryArtifacts:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._artifacts: dict[tuple[str, str], Artifact] = {}

    def save(self, artifact: Artifact) -> None:
        with self._lock:
            self._artifacts[(artifact.chat_id, artifact.artifact_id)] = artifact

    def get(self, chat_id: str, artifact_id: str) -> Artifact | None:
        with self._lock:
            return self._artifacts.get((chat_id, artifact_id))

    def list_by_chat(self, chat_id: str) -> list[Artifact]:
        with self._lock:
            return sorted(
                (a for (c, _), a in self._artifacts.items() if c == chat_id),
                key=lambda a: a.artifact_id,
            )

    def update_status(
        self, chat_id: str, artifact_id: str, expected: ArtifactStatus, new: ArtifactStatus
    ) -> Artifact:
        with self._lock:
            current = self._artifacts.get((chat_id, artifact_id))
            if current is None:
                raise LookupError(f"no artifact {artifact_id} in chat {chat_id}")
            if current.status != expected:
                raise StaleStatusError(expected, current.status)
            # Artifact has no updated_at: apart from status it is immutable.
            updated = current.model_copy(update={"status": ArtifactStatus(new)})
            self._artifacts[(chat_id, artifact_id)] = updated
            return updated


class InMemoryChats:
    def touch(self, user_id: str, chat_id: str, status: RequestStatus | None) -> None:
        with self._lock:
            chat = self._chats.get((user_id, chat_id))
            if chat is not None:
                self._chats[(user_id, chat_id)] = chat.model_copy(
                    update={"updated_at": now_iso(), "last_request_status": status}
                )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._chats: dict[tuple[str, str], Chat] = {}

    def save(self, chat: Chat) -> None:
        with self._lock:
            self._chats[(chat.user_id, chat.chat_id)] = chat

    def get(self, user_id: str, chat_id: str) -> Chat | None:
        with self._lock:
            return self._chats.get((user_id, chat_id))

    def list_by_user(self, user_id: str) -> list[Chat]:
        with self._lock:
            return sorted(
                (c for (u, _), c in self._chats.items() if u == user_id),
                # chat_id breaks updated_at ties so the order is deterministic.
                key=lambda c: (c.updated_at, c.chat_id),
                reverse=True,
            )


class InMemoryUsers:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}

    def save(self, user: User) -> None:
        with self._lock:
            self._users[user.user_id] = user

    def get(self, user_id: str) -> User | None:
        with self._lock:
            return self._users.get(user_id)


@dataclass
class Repositories:
    """The five repositories a service or the worker needs, typed by their Protocols."""

    messages: MessageRepository
    tasks: TaskRepository
    model_calls: ModelCallRepository
    artifacts: ArtifactRepository
    chats: ChatRepository
    users: UserRepository = field(default_factory=InMemoryUsers)

    @classmethod
    def in_memory(cls) -> "Repositories":
        return cls(
            messages=InMemoryMessages(),
            tasks=InMemoryTasks(),
            model_calls=InMemoryModelCalls(),
            artifacts=InMemoryArtifacts(),
            chats=InMemoryChats(),
        )
