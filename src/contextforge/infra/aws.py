"""DynamoDB/S3/SQS adapters. Existing dev resources are used; none are provisioned here.

Each adapter keeps the contract of its in-memory reference (``core/repositories/memory.py``,
``core/storage.py``): a missing record is a ``LookupError``, a status that moved on is a
``StaleStatusError``, and rules both backends answer the same way live in
``core/repositories/shared.py``.
"""

import hashlib
import json
import logging
import time
from dataclasses import asdict, fields
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from contextforge.core.entities import Entity
from contextforge.core.entities.artifact import Artifact, ArtifactStatus
from contextforge.core.entities.chat import Chat
from contextforge.core.entities.message import (
    Message,
    MessageAdapter,
    MessageKind,
    RequestMessage,
    RequestStatus,
)
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.task import Task, TaskResult, TaskStatus
from contextforge.core.entities.user import User
from contextforge.core.ids import now_iso
from contextforge.core.queue import WorkMessage
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.repositories.memory import Repositories
from contextforge.core.repositories.shared import sum_known_usage, unanswered
from contextforge.core.values import Usage
from contextforge.infra.boto_errors import error_code

logger = logging.getLogger(__name__)

# DynamoDB's item limit. Sizes are measured on the JSON encoding, which overstates DynamoDB's own
# count, so the guard trips before DynamoDB would.
MAX_ITEM_BYTES = 400 * 1024
# Attributes that index an item rather than describe the entity; reads drop them.
_INDEX_ATTRIBUTES = frozenset({"PK", "SK", "GSI1PK", "GSI1SK"})
_CONDITION_FAILED = "ConditionalCheckFailedException"


class ItemTooLargeError(ValueError):
    """An item, or the fields an update writes, over the item limit; refused before any call."""

    def __init__(self, pk: str, sk: str, size_bytes: int) -> None:
        super().__init__(
            f"{pk} {sk} is {size_bytes} bytes, over the {MAX_ITEM_BYTES}-byte DynamoDB item "
            "limit; offload large content to S3."
        )
        self.key = {"PK": pk, "SK": sk}
        self.size_bytes = size_bytes


def _plain(value: Any) -> Any:
    """Deserialised DynamoDB numbers (Decimal) back to int when integral, float otherwise."""
    return json.loads(json.dumps(value, default=lambda x: int(x) if x == int(x) else float(x)))


class DynamoStore:
    def __init__(self, client: Any, table: str) -> None:
        self.client, self.table = client, table
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()

    def _encode(self, pk: str, sk: str, attributes: dict[str, Any]) -> dict[str, Any]:
        """Serialise attributes for DynamoDB, refusing them over the item limit.

        boto3 rejects Python floats, so one JSON pass turns every float into a Decimal.
        """
        text = json.dumps(attributes)
        size = len(text.encode())
        if size > MAX_ITEM_BYTES:
            raise ItemTooLargeError(pk, sk, size)
        plain = json.loads(text, parse_float=Decimal)
        return {k: self.serializer.serialize(v) for k, v in plain.items()}

    def item(self, pk: str, sk: str, data: dict[str, Any]) -> dict[str, Any]:
        return self._encode(pk, sk, {"PK": pk, "SK": sk, **data})

    def key(self, pk: str, sk: str) -> dict[str, Any]:
        return {"PK": {"S": pk}, "SK": {"S": sk}}

    def decode(self, item: dict[str, Any]) -> dict[str, Any]:
        return dict(
            _plain(
                {
                    k: self.deserializer.deserialize(v)
                    for k, v in item.items()
                    if k not in _INDEX_ATTRIBUTES
                }
            )
        )

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        item = self.client.get_item(
            TableName=self.table, Key=self.key(pk, sk), ConsistentRead=True
        ).get("Item")
        return self.decode(item) if item else None

    def put(self, pk: str, sk: str, entity: Entity, extra: dict[str, str] | None = None) -> None:
        data = entity.model_dump(mode="json", by_alias=True) | (extra or {})
        self.client.put_item(TableName=self.table, Item=self.item(pk, sk, data))

    def query(self, pk: str, prefix: str) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "TableName": self.table,
            "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
            "ExpressionAttributeValues": {":pk": {"S": pk}, ":prefix": {"S": prefix}},
            "ConsistentRead": True,
        }
        rows: list[dict[str, Any]] = []
        while True:
            response = self.client.query(**params)
            rows.extend(self.decode(x) for x in response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return rows
            params["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def update_status(
        self,
        pk: str,
        sk: str,
        changes: dict[str, Any],
        expected: str,
        kind: str | None = None,
    ) -> dict[str, Any]:
        """Apply ``changes`` only while the stored status is ``expected`` (and kind is ``kind``).

        A missing record, or one of another kind, raises ``LookupError``; a record whose status
        moved on raises ``StaleStatusError``. The failed write returns the stored item, so telling
        the two apart needs no second read.
        """
        guards = {"status": expected} | ({"kind": kind} if kind is not None else {})
        try:
            return self._update(pk, sk, changes, guards)
        except ClientError as exc:
            if error_code(exc) != _CONDITION_FAILED:
                raise
            stored = self.decode(exc.response["Item"]) if exc.response.get("Item") else None
            if stored is None or (kind is not None and stored.get("kind") != kind):
                raise LookupError(f"no {kind or 'record'} at {pk} {sk}") from exc
            raise StaleStatusError(expected, str(stored.get("status"))) from exc

    def update_if_exists(self, pk: str, sk: str, changes: dict[str, Any]) -> None:
        """Apply ``changes`` to an existing record; do nothing when there is none."""
        try:
            self._update(pk, sk, changes, guards={})
        except ClientError as exc:
            if error_code(exc) != _CONDITION_FAILED:
                raise

    def _update(
        self, pk: str, sk: str, changes: dict[str, Any], guards: dict[str, str]
    ) -> dict[str, Any]:
        # Every name is a placeholder: status, name and result are DynamoDB reserved words.
        encoded = self._encode(pk, sk, changes)
        names = {f"#f{i}": name for i, name in enumerate(encoded)}
        values = {f":v{i}": value for i, value in enumerate(encoded.values())}
        # attribute_exists(PK) stops UpdateItem from creating a record that is not there.
        conditions = ["attribute_exists(PK)"]
        for name, value in guards.items():
            names[f"#{name}"] = name
            values[f":expected_{name}"] = {"S": str(value)}
            conditions.append(f"#{name} = :expected_{name}")
        response = self.client.update_item(
            TableName=self.table,
            Key=self.key(pk, sk),
            UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(encoded))),
            ConditionExpression=" AND ".join(conditions),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
        return self.decode(response["Attributes"])


class DynamoMessages:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save(self, message: Message) -> None:
        self.store.put(f"CHAT#{message.chat_id}", f"MSG#{message.message_id}", message)

    def get(self, chat_id: str, message_id: str) -> Message | None:
        raw = self.store.get(f"CHAT#{chat_id}", f"MSG#{message_id}")
        return MessageAdapter.validate_python(raw) if raw else None

    def list_by_chat(self, chat_id: str) -> list[Message]:
        return [
            MessageAdapter.validate_python(x) for x in self.store.query(f"CHAT#{chat_id}", "MSG#")
        ]

    def update_request_status(
        self,
        chat_id: str,
        message_id: str,
        expected: RequestStatus,
        new: RequestStatus,
        failure_reason: str | None = None,
    ) -> RequestMessage:
        changes: dict[str, Any] = {"status": new, "updatedAt": now_iso()}
        if failure_reason is not None:
            changes["failureReason"] = failure_reason
        return RequestMessage.model_validate(
            self.store.update_status(
                f"CHAT#{chat_id}",
                f"MSG#{message_id}",
                changes,
                str(expected),
                kind=MessageKind.REQUEST,
            )
        )

    def create_request(self, client_key: str, message: RequestMessage) -> RequestMessage:
        pk = "IDEMP#" + hashlib.sha256(client_key.encode()).hexdigest()
        pointer = {
            "chatId": message.chat_id,
            "messageId": message.message_id,
            "ttl": int(time.time()) + 86400,
        }
        try:
            self.store.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.store.table,
                            "Item": self.store.item(pk, "REQ", pointer),
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.store.table,
                            "Item": self.store.item(
                                f"CHAT#{message.chat_id}",
                                f"MSG#{message.message_id}",
                                message.model_dump(mode="json", by_alias=True),
                            ),
                        }
                    },
                ]
            )
            return message
        except ClientError as exc:
            if not _key_already_used(exc):
                raise
            existing = self.store.get(pk, "REQ")
            if existing is None:
                raise
            saved = self.get(existing["chatId"], existing["messageId"])
            if not isinstance(saved, RequestMessage):
                raise LookupError("Idempotent request no longer exists.") from exc
            return saved

    def pending_for_request(self, chat_id: str, request_message_id: str) -> list[Message]:
        return unanswered(self.list_by_chat(chat_id), request_message_id)


def _key_already_used(exc: ClientError) -> bool:
    """Whether a create_request transaction failed only because its idempotency key exists.

    The idempotency Put is the first item, so its cancellation reason comes first. Any other
    reason (a transaction conflict, a throttle, a validation error) is a real failure.
    """
    if error_code(exc) != "TransactionCanceledException":
        return False
    reasons = exc.response.get("CancellationReasons") or [{}]
    return bool(reasons[0].get("Code") == "ConditionalCheckFailed")


class DynamoTasks:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save_all(self, tasks: list[Task]) -> None:
        if not tasks:
            return
        self.store.client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": self.store.table,
                        "Item": self.store.item(
                            f"REQ#{t.request_message_id}",
                            f"TASK#{t.task_id}",
                            t.model_dump(mode="json", by_alias=True),
                        ),
                    }
                }
                for t in tasks
            ]
        )

    def save(self, task: Task) -> None:
        self.store.put(f"REQ#{task.request_message_id}", f"TASK#{task.task_id}", task)

    def get(self, request_message_id: str, task_id: str) -> Task | None:
        raw = self.store.get(f"REQ#{request_message_id}", f"TASK#{task_id}")
        return Task.model_validate(raw) if raw else None

    def list_by_request(self, request_message_id: str) -> list[Task]:
        return [
            Task.model_validate(x) for x in self.store.query(f"REQ#{request_message_id}", "TASK#")
        ]

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
        changes: dict[str, Any] = {"status": new, "updatedAt": now_iso()}
        if result is not None:
            changes["result"] = result.model_dump(mode="json", by_alias=True)
        if failure_reason is not None:
            changes["failureReason"] = failure_reason
        return Task.model_validate(
            self.store.update_status(
                f"REQ#{request_message_id}", f"TASK#{task_id}", changes, str(expected)
            )
        )


class DynamoCalls:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save(self, call: ModelCall) -> None:
        self.store.put(f"REQ#{call.request_message_id}", f"CALL#{call.model_call_id}", call)

    def get(self, request_message_id: str, model_call_id: str) -> ModelCall | None:
        raw = self.store.get(f"REQ#{request_message_id}", f"CALL#{model_call_id}")
        return ModelCall.model_validate(raw) if raw else None

    def list_by_request(self, request_message_id: str) -> list[ModelCall]:
        return [
            ModelCall.model_validate(x)
            for x in self.store.query(f"REQ#{request_message_id}", "CALL#")
        ]

    def sum_usage(self, request_message_id: str) -> Usage | None:
        return sum_known_usage(c.usage for c in self.list_by_request(request_message_id))


class DynamoArtifacts:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save(self, artifact: Artifact) -> None:
        # Spec 7.3: the sparse GSI1 holds Artifact items only, keyed by owner, for pattern 7.
        index = {"GSI1PK": f"USER#{artifact.user_id}", "GSI1SK": f"ART#{artifact.artifact_id}"}
        self.store.put(f"CHAT#{artifact.chat_id}", f"ART#{artifact.artifact_id}", artifact, index)

    def get(self, chat_id: str, artifact_id: str) -> Artifact | None:
        raw = self.store.get(f"CHAT#{chat_id}", f"ART#{artifact_id}")
        return Artifact.model_validate(raw) if raw else None

    def list_by_chat(self, chat_id: str) -> list[Artifact]:
        return [Artifact.model_validate(x) for x in self.store.query(f"CHAT#{chat_id}", "ART#")]

    def update_status(
        self, chat_id: str, artifact_id: str, expected: ArtifactStatus, new: ArtifactStatus
    ) -> Artifact:
        return Artifact.model_validate(
            self.store.update_status(
                f"CHAT#{chat_id}", f"ART#{artifact_id}", {"status": new}, str(expected)
            )
        )


class DynamoChats:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save(self, chat: Chat) -> None:
        self.store.put(f"USER#{chat.user_id}", f"CHAT#{chat.chat_id}", chat)

    def get(self, user_id: str, chat_id: str) -> Chat | None:
        raw = self.store.get(f"USER#{user_id}", f"CHAT#{chat_id}")
        return Chat.model_validate(raw) if raw else None

    def list_by_user(self, user_id: str) -> list[Chat]:
        return sorted(
            [Chat.model_validate(x) for x in self.store.query(f"USER#{user_id}", "CHAT#")],
            key=lambda c: (c.updated_at, c.chat_id),
            reverse=True,
        )

    def touch(self, user_id: str, chat_id: str, status: RequestStatus | None) -> None:
        self.store.update_if_exists(
            f"USER#{user_id}",
            f"CHAT#{chat_id}",
            {"updatedAt": now_iso(), "lastRequestStatus": status},
        )


class DynamoUsers:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save(self, user: User) -> None:
        self.store.put(f"USER#{user.user_id}", "PROFILE", user)

    def get(self, user_id: str) -> User | None:
        raw = self.store.get(f"USER#{user_id}", "PROFILE")
        return User.model_validate(raw) if raw else None


def dynamo_repositories(client: Any, table: str) -> Repositories:
    store = DynamoStore(client, table)
    return Repositories(
        messages=DynamoMessages(store),
        tasks=DynamoTasks(store),
        model_calls=DynamoCalls(store),
        artifacts=DynamoArtifacts(store),
        chats=DynamoChats(store),
        users=DynamoUsers(store),
    )


# Content types of the blob keys core/storage.py builds.
_CONTENT_TYPES = {
    ".csv": "text/csv",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".md": "text/markdown; charset=utf-8",
}


class S3Blobs:
    def __init__(self, client: Any, bucket: str) -> None:
        self.client, self.bucket = client, bucket

    def put(self, key: str, data: bytes) -> str:
        content_type = _CONTENT_TYPES.get(PurePosixPath(key).suffix, "application/octet-stream")
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{key}"

    def get(self, ref: str) -> bytes:
        prefix = f"s3://{self.bucket}/"
        if not ref.startswith(prefix):
            raise ValueError("Artifact does not belong to the configured bucket.")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=ref[len(prefix) :])
        except ClientError as exc:
            # A raw_result blob expires under the bucket's 30-day lifecycle rule.
            if error_code(exc) == "NoSuchKey":
                raise LookupError(f"no blob stored at {ref!r}") from exc
            raise
        body = response["Body"]
        try:
            return bytes(body.read())
        finally:
            body.close()


class QueueError(Exception):
    """A queue operation that cannot do what was asked, such as acking with an expired receipt."""


_WORK_FIELDS = frozenset(f.name for f in fields(WorkMessage)) - {"receipt"}
# Long polling: one ReceiveMessage waits up to 20 s (the SQS maximum) for work to arrive.
_WAIT_SECONDS = 20


def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _decode_work(body: str, receipt: str) -> WorkMessage | None:
    """The WorkMessage in an SQS body, or None when the body is not one."""
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.keys() <= _WORK_FIELDS:
        return None
    kind, reply = data.get("kind"), data.get("reply_message_id")
    valid = (
        kind in {"start", "resume"}
        and _is_id(data.get("chat_id"))
        and _is_id(data.get("message_id"))
        and (_is_id(reply) if kind == "resume" else reply is None or _is_id(reply))
    )
    return WorkMessage(**data, receipt=receipt) if valid else None


class SqsQueue:
    def __init__(self, client: Any, url: str, *, visibility_timeout_seconds: int = 1260) -> None:
        # A received message stays hidden this long. It must outlast the request deadline, or
        # SQS redelivers a request that is still running; the caller passes deadline + 60 s.
        self.client, self.url = client, url
        self.visibility_timeout_seconds = visibility_timeout_seconds

    def send(self, msg: WorkMessage) -> None:
        self.client.send_message(
            QueueUrl=self.url,
            MessageBody=json.dumps({k: v for k, v in asdict(msg).items() if k != "receipt"}),
        )

    def receive(self) -> WorkMessage | None:
        rows = self.client.receive_message(
            QueueUrl=self.url,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=_WAIT_SECONDS,
            VisibilityTimeout=self.visibility_timeout_seconds,
        ).get("Messages", [])
        if not rows:
            return None
        row = rows[0]
        work = _decode_work(row["Body"], row["ReceiptHandle"])
        if work is None:
            self._reject(row)
        return work

    def _reject(self, row: dict[str, Any]) -> None:
        """Leave a malformed message for the DLQ: never acked, and visible again at once.

        The redrive policy moves it after the queue's maxReceiveCount receives. Released at
        once, those take seconds; held for the receive's visibility timeout (the request
        deadline plus a minute) they took most of an hour. The body is untrusted, so only its
        id is logged.
        """
        logger.warning("Malformed work message %s left for the DLQ", row.get("MessageId"))
        try:
            self.client.change_message_visibility(
                QueueUrl=self.url, ReceiptHandle=row["ReceiptHandle"], VisibilityTimeout=0
            )
        except ClientError as exc:
            # Still never acked: it reaches the DLQ, only later.
            logger.warning(
                "Could not release malformed message %s (%s); it stays hidden until its "
                "visibility timeout ends",
                row.get("MessageId"),
                error_code(exc),
            )

    def ack(self, msg: WorkMessage) -> None:
        if not msg.receipt:
            return
        try:
            self.client.delete_message(QueueUrl=self.url, ReceiptHandle=msg.receipt)
        except ClientError as exc:
            if error_code(exc) in {"ReceiptHandleIsInvalid", "InvalidParameterValue"}:
                raise QueueError(
                    f"Cannot ack work for request {msg.message_id}: its receipt expired after "
                    f"{self.visibility_timeout_seconds} s, so SQS will deliver it again."
                ) from exc
            raise
