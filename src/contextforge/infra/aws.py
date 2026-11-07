"""DynamoDB/S3/SQS adapters. Existing dev resources are used; none are provisioned here."""

import hashlib
import json
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer  # type: ignore[import-untyped]

from contextforge.core.entities.artifact import Artifact, ArtifactStatus
from contextforge.core.entities.chat import Chat
from contextforge.core.entities.message import (
    Message,
    MessageAdapter,
    RequestMessage,
    RequestStatus,
)
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.task import Task, TaskResult, TaskStatus
from contextforge.core.entities.user import User
from contextforge.core.ids import now_iso
from contextforge.core.queue import WorkMessage
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.repositories.memory import Repositories, _sum_known_usage
from contextforge.core.values import Usage


def _plain(value: Any) -> Any:
    return json.loads(json.dumps(value, default=lambda x: int(x) if x == int(x) else float(x)))


class DynamoStore:
    def __init__(self, client: Any, table: str) -> None:
        self.client, self.table = client, table
        self.serializer, self.deserializer = TypeSerializer(), TypeDeserializer()

    def item(self, pk: str, sk: str, data: dict[str, Any]) -> dict[str, Any]:
        plain = json.loads(json.dumps({"PK": pk, "SK": sk, **data}), parse_float=Decimal)
        if len(json.dumps(data).encode()) > 300_000:
            raise ValueError("Record exceeds the operational record budget; offload large content.")
        return {k: self.serializer.serialize(v) for k, v in plain.items()}

    def key(self, pk: str, sk: str) -> dict[str, Any]:
        return {"PK": {"S": pk}, "SK": {"S": sk}}

    def decode(self, item: dict[str, Any]) -> dict[str, Any]:
        return dict(
            _plain(
                {
                    k: self.deserializer.deserialize(v)
                    for k, v in item.items()
                    if k not in {"PK", "SK", "GSI1PK", "GSI1SK"}
                }
            )
        )

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        item = self.client.get_item(
            TableName=self.table, Key=self.key(pk, sk), ConsistentRead=True
        ).get("Item")
        return self.decode(item) if item else None

    def put(self, pk: str, sk: str, entity: Any) -> None:
        self.client.put_item(
            TableName=self.table,
            Item=self.item(pk, sk, entity.model_dump(mode="json", by_alias=True)),
        )

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

    def update(
        self, pk: str, sk: str, fields: dict[str, Any], expected: str | None = None
    ) -> dict[str, Any]:
        names = {f"#f{i}": k for i, k in enumerate(fields)}
        values = {
            f":v{i}": self.serializer.serialize(json.loads(json.dumps(v), parse_float=Decimal))
            for i, v in enumerate(fields.values())
        }
        params: dict[str, Any] = {
            "TableName": self.table,
            "Key": self.key(pk, sk),
            "UpdateExpression": "SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(fields))),
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
            "ReturnValues": "ALL_NEW",
        }
        params["ConditionExpression"] = "attribute_exists(PK)"
        if expected is not None:
            names["#status"] = "status"
            values[":expected"] = {"S": expected}
            params["ConditionExpression"] += " AND #status = :expected"
        try:
            return self.decode(self.client.update_item(**params)["Attributes"])
        except Exception as exc:
            if (
                getattr(exc, "response", {}).get("Error", {}).get("Code")
                == "ConditionalCheckFailedException"
            ):
                actual = self.get(pk, sk)
                raise StaleStatusError(
                    expected or "existing", actual.get("status", "missing") if actual else "missing"
                ) from exc
            raise


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
        fields: dict[str, Any] = {"status": new, "updatedAt": now_iso()}
        if failure_reason is not None:
            fields["failureReason"] = failure_reason
        return RequestMessage.model_validate(
            self.store.update(f"CHAT#{chat_id}", f"MSG#{message_id}", fields, str(expected))
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
        except Exception as exc:
            if (
                getattr(exc, "response", {}).get("Error", {}).get("Code")
                != "TransactionCanceledException"
            ):
                raise
            existing = self.store.get(pk, "REQ")
            if existing is None:
                raise
            saved = self.get(existing["chatId"], existing["messageId"])
            if not isinstance(saved, RequestMessage):
                raise LookupError("Idempotent request no longer exists.") from exc
            return saved

    def pending_for_request(self, chat_id: str, request_message_id: str) -> list[Message]:
        rows = self.list_by_chat(chat_id)
        answered = {getattr(m, "in_reply_to_message_id", None) for m in rows}
        return [
            m
            for m in rows
            if m.kind in {"clarification", "proposed_action"}
            and getattr(m, "request_message_id", None) == request_message_id
            and m.message_id not in answered
        ]


class DynamoTasks:
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

    def __init__(self, store: DynamoStore) -> None:
        self.store = store

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
        fields: dict[str, Any] = {"status": new, "updatedAt": now_iso()}
        if result is not None:
            fields["result"] = result.model_dump(mode="json", by_alias=True)
        if failure_reason is not None:
            fields["failureReason"] = failure_reason
        return Task.model_validate(
            self.store.update(f"REQ#{request_message_id}", f"TASK#{task_id}", fields, str(expected))
        )


class DynamoCalls:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save(self, call: ModelCall) -> None:
        self.store.put(f"REQ#{call.request_message_id}", f"CALL#{call.model_call_id}", call)

    def list_by_request(self, request_message_id: str) -> list[ModelCall]:
        return [
            ModelCall.model_validate(x)
            for x in self.store.query(f"REQ#{request_message_id}", "CALL#")
        ]

    def sum_usage(self, request_message_id: str) -> Usage | None:
        return _sum_known_usage(c.usage for c in self.list_by_request(request_message_id))


class DynamoArtifacts:
    def __init__(self, store: DynamoStore) -> None:
        self.store = store

    def save(self, artifact: Artifact) -> None:
        self.store.put(f"CHAT#{artifact.chat_id}", f"ART#{artifact.artifact_id}", artifact)

    def get(self, chat_id: str, artifact_id: str) -> Artifact | None:
        raw = self.store.get(f"CHAT#{chat_id}", f"ART#{artifact_id}")
        return Artifact.model_validate(raw) if raw else None

    def list_by_chat(self, chat_id: str) -> list[Artifact]:
        return [Artifact.model_validate(x) for x in self.store.query(f"CHAT#{chat_id}", "ART#")]

    def update_status(
        self, chat_id: str, artifact_id: str, expected: ArtifactStatus, new: ArtifactStatus
    ) -> Artifact:
        return Artifact.model_validate(
            self.store.update(
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
        self.store.update(
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


class S3Blobs:
    def __init__(self, client: Any, bucket: str) -> None:
        self.client, self.bucket = client, bucket

    def put(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)
        return f"s3://{self.bucket}/{key}"

    def get(self, ref: str) -> bytes:
        prefix = f"s3://{self.bucket}/"
        if not ref.startswith(prefix):
            raise ValueError("Artifact does not belong to the configured bucket.")
        body = self.client.get_object(Bucket=self.bucket, Key=ref[len(prefix) :])["Body"]
        try:
            return bytes(body.read())
        finally:
            body.close()


class SqsQueue:
    def __init__(self, client: Any, url: str) -> None:
        self.client, self.url = client, url

    def send(self, msg: WorkMessage) -> None:
        self.client.send_message(
            QueueUrl=self.url,
            MessageBody=json.dumps({k: v for k, v in asdict(msg).items() if k != "receipt"}),
        )

    def receive(self) -> WorkMessage | None:
        rows = self.client.receive_message(
            QueueUrl=self.url, MaxNumberOfMessages=1, WaitTimeSeconds=1
        ).get("Messages", [])
        if not rows:
            return None
        row = rows[0]
        try:
            data = json.loads(row["Body"])
            if data.get("kind") not in {"start", "resume"} or not all(
                isinstance(data.get(k), str) and data[k] for k in ("chat_id", "message_id")
            ):
                return None  # Do not acknowledge: SQS redrive policy sends malformed work to DLQ.
            if data["kind"] == "resume" and not isinstance(data.get("reply_message_id"), str):
                return None
            return WorkMessage(**data, receipt=row["ReceiptHandle"])
        except (ValueError, TypeError, KeyError):
            return None

    def ack(self, msg: WorkMessage) -> None:
        if msg.receipt:
            self.client.delete_message(QueueUrl=self.url, ReceiptHandle=msg.receipt)
