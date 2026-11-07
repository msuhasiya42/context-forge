"""Repository Protocols (implementation plan section 7): what callers may ask of storage.

Repositories store and return entity objects, never dicts. ``save`` is an upsert keyed by the
entity's id. ``update_status`` methods are conditional writes: they apply only when the stored
status equals ``expected`` and raise ``StaleStatusError`` otherwise, so a stale worker cannot
overwrite a newer state. Optional ``result`` and ``failure_reason`` arguments change the stored
field only when given. Updating a record that does not exist raises ``LookupError``.
"""

from typing import Protocol

from contextforge.core.entities.artifact import Artifact, ArtifactStatus
from contextforge.core.entities.chat import Chat
from contextforge.core.entities.message import Message, RequestMessage, RequestStatus
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.task import Task, TaskResult, TaskStatus
from contextforge.core.entities.user import User
from contextforge.core.values import Usage


class StaleStatusError(Exception):
    """A conditional update found a status other than the one the caller expected."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"expected status {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


class MessageRepository(Protocol):
    def save(self, message: Message) -> None: ...

    def get(self, chat_id: str, message_id: str) -> Message | None: ...

    def list_by_chat(self, chat_id: str) -> list[Message]:
        """Every Message in the chat, ordered by ``message_id`` (creation order)."""
        ...

    def update_request_status(
        self,
        chat_id: str,
        message_id: str,
        expected: RequestStatus,
        new: RequestStatus,
        failure_reason: str | None = None,
    ) -> RequestMessage: ...

    def create_request(self, client_key: str, message: RequestMessage) -> RequestMessage:
        """Store ``message`` once per ``client_key``.

        A replayed key returns the message stored the first time and stores nothing new.
        """
        ...

    def pending_for_request(self, chat_id: str, request_message_id: str) -> list[Message]:
        """Clarification and proposed_action Messages of the request with no reply yet.

        A reply is a clarification_response or action_response in the chat whose
        ``in_reply_to_message_id`` names the entry. Ordered by ``message_id``.
        """
        ...


class TaskRepository(Protocol):
    def save_all(self, tasks: list[Task]) -> None: ...

    def save(self, task: Task) -> None: ...

    def get(self, request_message_id: str, task_id: str) -> Task | None: ...

    def list_by_request(self, request_message_id: str) -> list[Task]:
        """Every Task of the request, ordered by ``task_id``."""
        ...

    def update_status(
        self,
        request_message_id: str,
        task_id: str,
        expected: TaskStatus,
        new: TaskStatus,
        *,
        result: TaskResult | None = None,
        failure_reason: str | None = None,
    ) -> Task: ...


class ModelCallRepository(Protocol):
    def save(self, call: ModelCall) -> None: ...

    def get(self, request_message_id: str, model_call_id: str) -> ModelCall | None: ...

    def list_by_request(self, request_message_id: str) -> list[ModelCall]:
        """Every ModelCall of the request, ordered by ``model_call_id``."""
        ...

    def sum_usage(self, request_message_id: str) -> Usage | None:
        """Total usage over the calls that reported it; None when none did (unknown is not 0)."""
        ...


class ArtifactRepository(Protocol):
    def save(self, artifact: Artifact) -> None: ...

    def get(self, chat_id: str, artifact_id: str) -> Artifact | None: ...

    def list_by_chat(self, chat_id: str) -> list[Artifact]:
        """Every Artifact in the chat, ordered by ``artifact_id``."""
        ...

    def update_status(
        self, chat_id: str, artifact_id: str, expected: ArtifactStatus, new: ArtifactStatus
    ) -> Artifact: ...


class ChatRepository(Protocol):
    def touch(self, user_id: str, chat_id: str, status: RequestStatus | None) -> None: ...

    def save(self, chat: Chat) -> None: ...

    def get(self, user_id: str, chat_id: str) -> Chat | None: ...

    def list_by_user(self, user_id: str) -> list[Chat]:
        """The user's chats, most recently updated first."""
        ...


class UserRepository(Protocol):
    def save(self, user: User) -> None: ...
    def get(self, user_id: str) -> User | None: ...
