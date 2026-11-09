"""AWS adapters against botocore Stubber: every call's parameters are asserted, nothing leaves
the process. The behaviour checked is the in-memory reference contract (tests/test_repositories.py)
plus the DynamoDB, S3 and SQS details the memory backend has no equivalent for."""

import json
import logging
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.stub import ANY, Stubber

from contextforge.core.entities import Entity
from contextforge.core.entities.artifact import Artifact
from contextforge.core.entities.chat import Chat
from contextforge.core.entities.message import (
    ClarificationMessage,
    ClarificationResponseMessage,
    RequestMessage,
    RequestStatus,
)
from contextforge.core.entities.model_call import ModelCall
from contextforge.core.entities.task import Task, TaskResult
from contextforge.core.queue import WorkMessage
from contextforge.core.repositories.base import StaleStatusError
from contextforge.core.values import Usage
from contextforge.infra.aws import (
    MAX_ITEM_BYTES,
    DynamoArtifacts,
    DynamoCalls,
    DynamoChats,
    DynamoMessages,
    DynamoStore,
    DynamoTasks,
    ItemTooLargeError,
    QueueError,
    S3Blobs,
    SqsQueue,
)
from tests.helpers import t1_result

TABLE = "contextforge"
BUCKET = "test-bucket"
QUEUE_URL = "https://sqs.ap-southeast-2.amazonaws.com/123/work"
CONDITION_FAILED = "ConditionalCheckFailedException"


def client(service: str) -> Any:
    return boto3.client(
        service,
        region_name="ap-southeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture
def db() -> Any:
    return client("dynamodb")


@pytest.fixture
def store(db: Any) -> DynamoStore:
    return DynamoStore(db, TABLE)


def key(pk: str, sk: str) -> dict[str, Any]:
    return {"PK": {"S": pk}, "SK": {"S": sk}}


def item(store: DynamoStore, pk: str, sk: str, entity: Entity, **extra: str) -> dict[str, Any]:
    return store.item(pk, sk, entity.model_dump(mode="json", by_alias=True) | extra)


def query_params(pk: str, prefix: str) -> dict[str, Any]:
    return {
        "TableName": TABLE,
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :prefix)",
        "ExpressionAttributeValues": {":pk": {"S": pk}, ":prefix": {"S": prefix}},
        "ConsistentRead": True,
    }


def request(status: RequestStatus = RequestStatus.ACCEPTED, message_id: str = "m1") -> Any:
    return RequestMessage.model_validate(
        {
            "kind": "request",
            "messageId": message_id,
            "chatId": "c1",
            "userId": "u1",
            "sender": "user",
            "content": "Compare October with September",
            "status": status,
            "createdAt": "t",
            "updatedAt": "t",
        }
    )


def clarification(message_id: str) -> ClarificationMessage:
    return ClarificationMessage.model_validate(
        {
            "kind": "clarification",
            "messageId": message_id,
            "chatId": "c1",
            "userId": "u1",
            "sender": "assistant",
            "content": "Which account?",
            "requestMessageId": "m1",
            "createdAt": "t",
            "updatedAt": "t",
        }
    )


def task(task_id: str = "t1") -> Task:
    return Task.model_validate(
        {
            "taskId": task_id,
            "requestMessageId": "m1",
            "kind": "web_search",
            "goal": "Research",
            "inputArtifactIds": [],
            "status": "PLANNED",
            "attempt": 1,
            "createdAt": "t",
            "updatedAt": "t",
        }
    )


# DynamoDB


def test_web_tool_admission_is_atomic_and_reads_legacy_requests(db, store) -> None:
    params = {
        "TableName": TABLE,
        "Key": key("CHAT#c1", "MSG#m1"),
        "UpdateExpression": "SET #count = if_not_exists(#count, :zero) + :one",
        "ConditionExpression": (
            "attribute_exists(PK) AND #kind = :kind AND "
            "(attribute_not_exists(#count) OR #count < :limit)"
        ),
        "ExpressionAttributeNames": {"#count": "webToolCalls", "#kind": "kind"},
        "ExpressionAttributeValues": {
            ":zero": {"N": "0"},
            ":one": {"N": "1"},
            ":limit": {"N": "50"},
            ":kind": {"S": "request"},
        },
        "ReturnValues": "UPDATED_NEW",
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }
    capped = item(store, "CHAT#c1", "MSG#m1", request().model_copy(update={"web_tool_calls": 50}))
    with Stubber(db) as stub:
        stub.add_response("update_item", {"Attributes": {"webToolCalls": {"N": "1"}}}, params)
        stub.add_response("update_item", {"Attributes": {"webToolCalls": {"N": "50"}}}, params)
        stub.add_client_error(
            "update_item", CONDITION_FAILED, expected_params=params, modeled_fields={"Item": capped}
        )
        assert DynamoMessages(store).reserve_web_tool_call("c1", "m1", 50) == 1
        # A reconstructed adapter uses the saved counter, never a process-local reset.
        assert DynamoMessages(store).reserve_web_tool_call("c1", "m1", 50) == 50
        assert DynamoMessages(store).reserve_web_tool_call("c1", "m1", 50) is None
        stub.assert_no_pending_responses()


def test_request_status_update_is_conditional_and_a_moved_status_is_stale(db, store) -> None:
    conditional_update = {
        "TableName": TABLE,
        "Key": key("CHAT#c1", "MSG#m1"),
        "UpdateExpression": "SET #f0 = :v0, #f1 = :v1",
        "ConditionExpression": (
            "attribute_exists(PK) AND #status = :expected_status AND #kind = :expected_kind"
        ),
        "ExpressionAttributeNames": {
            "#f0": "status",
            "#f1": "updatedAt",
            "#status": "status",
            "#kind": "kind",
        },
        "ExpressionAttributeValues": {
            ":v0": {"S": "RUNNING"},
            ":v1": {"S": ANY},
            ":expected_status": {"S": "ACCEPTED"},
            ":expected_kind": {"S": "request"},
        },
        "ReturnValues": "ALL_NEW",
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }
    running = item(store, "CHAT#c1", "MSG#m1", request(RequestStatus.RUNNING))
    cancelled = item(store, "CHAT#c1", "MSG#m1", request(RequestStatus.CANCELLED))
    messages = DynamoMessages(store)
    with Stubber(db) as stub:
        stub.add_response("update_item", {"Attributes": running}, conditional_update)
        # The failed write hands back the stored item, so no GetItem follows.
        stub.add_client_error(
            "update_item",
            service_error_code=CONDITION_FAILED,
            expected_params=conditional_update,
            modeled_fields={"Item": cancelled},
        )
        updated = messages.update_request_status("c1", "m1", "ACCEPTED", "RUNNING")
        with pytest.raises(StaleStatusError) as stale:
            messages.update_request_status("c1", "m1", "ACCEPTED", "RUNNING")
        stub.assert_no_pending_responses()
    assert updated.status == RequestStatus.RUNNING
    assert (stale.value.expected, stale.value.actual) == ("ACCEPTED", "CANCELLED")


def test_status_update_on_a_missing_record_or_another_kind_is_lookup_error(db, store) -> None:
    with Stubber(db) as stub:
        for _ in range(3):
            stub.add_client_error("update_item", service_error_code=CONDITION_FAILED)
        stub.add_client_error(
            "update_item",
            service_error_code=CONDITION_FAILED,
            modeled_fields={"Item": item(store, "CHAT#c1", "MSG#q1", clarification("q1"))},
        )
        with pytest.raises(LookupError):
            DynamoMessages(store).update_request_status("c1", "nope", "ACCEPTED", "RUNNING")
        with pytest.raises(LookupError):
            DynamoTasks(store).update_status("m1", "nope", "PLANNED", "RUNNING")
        with pytest.raises(LookupError):
            DynamoArtifacts(store).update_status("c1", "nope", "UPLOADING", "AVAILABLE")
        with pytest.raises(LookupError):  # a clarification is not a request Message
            DynamoMessages(store).update_request_status("c1", "q1", "ACCEPTED", "RUNNING")
        stub.assert_no_pending_responses()


def test_a_read_timeout_is_raised_as_itself_not_as_a_condition_failure(db, store) -> None:
    def time_out(**_: Any) -> None:
        raise ReadTimeoutError(endpoint_url="https://dynamodb.ap-southeast-2.amazonaws.com")

    with Stubber(db) as stub:
        # Runs ahead of the Stubber's own before-call handler, where the HTTP send would be.
        db.meta.events.register_first("before-call.*.*", time_out)
        stub.add_response("update_item", {"Attributes": {}})
        with pytest.raises(ReadTimeoutError):
            DynamoTasks(store).update_status("m1", "t1", "PLANNED", "RUNNING")


def test_an_oversized_update_is_refused_before_any_call(db, store) -> None:
    result = TaskResult.model_validate({**t1_result(), "summary": "x" * MAX_ITEM_BYTES})
    with Stubber(db), pytest.raises(ItemTooLargeError) as too_large:
        DynamoTasks(store).update_status("m1", "t1", "RUNNING", "COMPLETED", result=result)
    assert too_large.value.key == {"PK": "REQ#m1", "SK": "TASK#t1"}


def test_create_request_treats_only_a_used_idempotency_key_as_a_replay(db, store) -> None:
    first, retry = request(), request(message_id="m2")
    idempotency = key("IDEMP#" + sha256(b"k1").hexdigest(), "REQ")
    transaction = {
        "TransactItems": [
            {
                "Put": {
                    "TableName": TABLE,
                    "Item": ANY,  # the pointer's ttl moves with the clock
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {"Put": {"TableName": TABLE, "Item": item(store, "CHAT#c1", "MSG#m2", retry)}},
        ]
    }

    def cancelled(first_reason: str) -> dict[str, Any]:
        return {"CancellationReasons": [{"Code": first_reason}, {"Code": "None"}]}

    messages = DynamoMessages(store)
    with Stubber(db) as stub:
        stub.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            expected_params=transaction,
            modeled_fields=cancelled("ConditionalCheckFailed"),
        )
        stub.add_response(
            "get_item",
            {"Item": {"chatId": {"S": "c1"}, "messageId": {"S": "m1"}}},
            {"TableName": TABLE, "Key": idempotency, "ConsistentRead": True},
        )
        stub.add_response(
            "get_item",
            {"Item": item(store, "CHAT#c1", "MSG#m1", first)},
            {"TableName": TABLE, "Key": key("CHAT#c1", "MSG#m1"), "ConsistentRead": True},
        )
        stub.add_client_error(
            "transact_write_items",
            service_error_code="TransactionCanceledException",
            expected_params=transaction,
            modeled_fields=cancelled("TransactionConflict"),
        )
        assert messages.create_request("k1", retry) == first
        with pytest.raises(ClientError):
            messages.create_request("k1", retry)
        stub.assert_no_pending_responses()


def test_floats_round_trip_through_dynamodb_numbers(db, store) -> None:
    call = ModelCall.model_validate(
        {
            "modelCallId": "k1",
            "requestMessageId": "m1",
            "stage": "plan",
            "model": "bedrock/amazon.nova-pro-v1:0",
            "contextVersion": "mem:1|prompt:plan.1",
            "usage": Usage(input_tokens=4200, output_tokens=610),
            "estimatedCostUsd": 0.0183,
            "priceVersion": "2026-11-07",
            "outcome": "COMPLETED",
            # F9: an int stays an int, a list of strings a list
            "metadata": {"output_path": "tool", "reasoning_blocks": 1, "lenient_fixes": ["x"]},
            "startedAt": "t",
        }
    )
    finished = task().model_copy(update={"result": TaskResult.model_validate(t1_result())})
    call_item = item(store, "REQ#m1", "CALL#k1", call)
    task_item = item(store, "REQ#m1", "TASK#t1", finished)
    assert call_item["estimatedCostUsd"] == {"N": "0.0183"}
    with Stubber(db) as stub:
        stub.add_response("put_item", {}, {"TableName": TABLE, "Item": call_item})
        stub.add_response("query", {"Items": [call_item]}, query_params("REQ#m1", "CALL#"))
        stub.add_response(
            "get_item",
            {"Item": task_item},
            {"TableName": TABLE, "Key": key("REQ#m1", "TASK#t1"), "ConsistentRead": True},
        )
        DynamoCalls(store).save(call)
        assert DynamoCalls(store).list_by_request("m1") == [call]
        loaded = DynamoTasks(store).get("m1", "t1")
    assert loaded == finished and loaded.result is not None
    amount = loaded.result.structured_data["transferPairs"][0]["amount"]
    assert amount == 25000.0 and not isinstance(amount, Decimal)


def test_a_model_call_keeps_its_transcript_ref_and_is_read_back_by_id(db, store) -> None:
    call = ModelCall.model_validate(
        {
            "modelCallId": "k1",
            "requestMessageId": "m1",
            "stage": "execute",
            "model": "bedrock/amazon.nova-lite-v1:0",
            "contextVersion": "mem:1|prompt:execute.5",
            "usage": None,
            "priceVersion": "2026-11-07",
            "transcriptRef": "s3://test-bucket/transcripts/m1/k1.json",
            "outcome": "COMPLETED",
            "startedAt": "2026-11-07T10:00:00.120Z",
        }
    )
    call_item = item(store, "REQ#m1", "CALL#k1", call)
    assert call_item["transcriptRef"] == {"S": "s3://test-bucket/transcripts/m1/k1.json"}
    # A record written before transcripts existed has no attribute, and reads as None.
    older = {k: v for k, v in call_item.items() if k != "transcriptRef"}
    get = {"TableName": TABLE, "Key": key("REQ#m1", "CALL#k1"), "ConsistentRead": True}
    with Stubber(db) as stub:
        stub.add_response("put_item", {}, {"TableName": TABLE, "Item": call_item})
        stub.add_response("get_item", {"Item": call_item}, get)
        stub.add_response("get_item", {"Item": older}, get)
        stub.add_response("get_item", {}, {**get, "Key": key("REQ#m1", "CALL#none")})
        calls = DynamoCalls(store)
        calls.save(call)
        assert calls.get("m1", "k1") == call
        assert calls.get("m1", "k1").transcript_ref is None
        assert calls.get("m1", "none") is None
        stub.assert_no_pending_responses()


def test_pending_for_request_reads_every_page_of_the_chat(db, store) -> None:
    answered, still_open = clarification("m2"), clarification("m3")
    reply = ClarificationResponseMessage.model_validate(
        answered.model_dump(by_alias=True)
        | {
            "kind": "clarification_response",
            "messageId": "m4",
            "sender": "user",
            "inReplyToMessageId": "m2",
        }
    )
    page_end = key("CHAT#c1", "MSG#m3")
    first_page = [item(store, "CHAT#c1", f"MSG#{m.message_id}", m) for m in (answered, still_open)]
    with Stubber(db) as stub:
        stub.add_response(
            "query",
            {"Items": first_page, "LastEvaluatedKey": page_end},
            query_params("CHAT#c1", "MSG#"),
        )
        stub.add_response(
            "query",
            {"Items": [item(store, "CHAT#c1", "MSG#m4", reply)]},
            query_params("CHAT#c1", "MSG#") | {"ExclusiveStartKey": page_end},
        )
        pending = DynamoMessages(store).pending_for_request("c1", "m1")
    assert [m.message_id for m in pending] == ["m3"]


def test_artifact_put_carries_the_gsi1_keys_and_reads_drop_them(db, store) -> None:
    artifact = Artifact.model_validate(
        {
            "artifactId": "a1",
            "userId": "u1",
            "chatId": "c1",
            "kind": "raw_result",
            "name": "example.com",
            "mediaType": "application/json",
            "sizeBytes": 10,
            "storageRef": f"s3://{BUCKET}/raw_result/c1/a1.jsonl",
            "status": "AVAILABLE",
            "indexStatus": "NOT_INDEXED",
            "createdAt": "t",
        }
    )
    indexed = item(store, "CHAT#c1", "ART#a1", artifact, GSI1PK="USER#u1", GSI1SK="ART#a1")
    with Stubber(db) as stub:
        stub.add_response("put_item", {}, {"TableName": TABLE, "Item": indexed})
        stub.add_response(
            "get_item",
            {"Item": indexed},
            {"TableName": TABLE, "Key": key("CHAT#c1", "ART#a1"), "ConsistentRead": True},
        )
        DynamoArtifacts(store).save(artifact)
        assert DynamoArtifacts(store).get("c1", "a1") == artifact


def test_touch_updates_an_existing_chat_and_ignores_a_missing_one(db, store) -> None:
    def touch_params(chat_id: str) -> dict[str, Any]:
        return {
            "TableName": TABLE,
            "Key": key("USER#u1", f"CHAT#{chat_id}"),
            "UpdateExpression": "SET #f0 = :v0, #f1 = :v1",
            "ConditionExpression": "attribute_exists(PK)",
            "ExpressionAttributeNames": {"#f0": "updatedAt", "#f1": "lastRequestStatus"},
            "ExpressionAttributeValues": {":v0": {"S": ANY}, ":v1": {"S": "COMPLETED"}},
            "ReturnValues": "ALL_NEW",
            "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
        }

    chat = Chat.model_validate(
        {
            "chatId": "c1",
            "userId": "u1",
            "title": "Research",
            "status": "ACTIVE",
            "createdAt": "t",
            "updatedAt": "t2",
            "lastRequestStatus": "COMPLETED",
        }
    )
    chats = DynamoChats(store)
    with Stubber(db) as stub:
        stub.add_response(
            "update_item",
            {"Attributes": item(store, "USER#u1", "CHAT#c1", chat)},
            touch_params("c1"),
        )
        stub.add_client_error(
            "update_item", service_error_code=CONDITION_FAILED, expected_params=touch_params("gone")
        )
        chats.touch("u1", "c1", RequestStatus.COMPLETED)
        chats.touch("u1", "gone", RequestStatus.COMPLETED)  # no chat, nothing to do
        stub.assert_no_pending_responses()


def test_plan_is_saved_in_one_dynamodb_transaction(db, store) -> None:
    tasks = [task("t1"), task("t2")]
    expected = {
        "TransactItems": [
            {"Put": {"TableName": TABLE, "Item": item(store, "REQ#m1", f"TASK#{t.task_id}", t)}}
            for t in tasks
        ]
    }
    with Stubber(db) as stub:
        stub.add_response("transact_write_items", {}, expected)
        DynamoTasks(store).save_all(tasks)
        stub.assert_no_pending_responses()


# S3


def test_s3_put_names_the_content_type_and_get_returns_the_bytes() -> None:
    s3 = client("s3")
    blobs = S3Blobs(s3, BUCKET)
    key_name = "raw_result/c1/a1.jsonl"
    with Stubber(s3) as stub:
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": BUCKET,
                "Key": key_name,
                "Body": b"hello",
                "ContentType": "application/x-ndjson",
            },
        )
        stub.add_response(
            "get_object", {"Body": BytesIO(b"hello")}, {"Bucket": BUCKET, "Key": key_name}
        )
        ref = blobs.put(key_name, b"hello")
        assert ref == f"s3://{BUCKET}/{key_name}"
        assert blobs.get(ref) == b"hello"


def test_s3_missing_key_is_lookup_error_like_the_memory_store() -> None:
    s3 = client("s3")
    with Stubber(s3) as stub:
        # A raw_result blob past the bucket's 30-day lifecycle rule.
        stub.add_client_error(
            "get_object",
            service_error_code="NoSuchKey",
            http_status_code=404,
            expected_params={"Bucket": BUCKET, "Key": "raw_result/c1/gone.jsonl"},
        )
        with pytest.raises(LookupError):
            S3Blobs(s3, BUCKET).get(f"s3://{BUCKET}/raw_result/c1/gone.jsonl")


# SQS


def receive_params(visibility: int = 1260) -> dict[str, Any]:
    return {
        "QueueUrl": QUEUE_URL,
        "MaxNumberOfMessages": 1,
        "WaitTimeSeconds": 20,
        "VisibilityTimeout": visibility,
    }


def test_sqs_send_receive_and_ack_by_that_receipt() -> None:
    sqs = client("sqs")
    queue = SqsQueue(sqs, QUEUE_URL, visibility_timeout_seconds=960)
    work = WorkMessage(kind="resume", chat_id="c1", message_id="m1", reply_message_id="m4")
    body = json.dumps(
        {"kind": "resume", "chat_id": "c1", "message_id": "m1", "reply_message_id": "m4"}
    )
    with Stubber(sqs) as stub:
        stub.add_response("send_message", {}, {"QueueUrl": QUEUE_URL, "MessageBody": body})
        stub.add_response(
            "receive_message",
            {"Messages": [{"MessageId": "id1", "ReceiptHandle": "receipt-1", "Body": body}]},
            receive_params(visibility=960),
        )
        stub.add_response(
            "delete_message", {}, {"QueueUrl": QUEUE_URL, "ReceiptHandle": "receipt-1"}
        )
        queue.send(work)
        received = queue.receive()
        assert received == work and received.receipt == "receipt-1"
        queue.ack(received)
        stub.assert_no_pending_responses()


def test_malformed_bodies_are_released_at_once_for_the_dead_letter_queue(caplog) -> None:
    """Never acked, and made visible again at once, so the redrive policy moves the message
    to the DLQ after maxReceiveCount quick receives rather than one visibility timeout each
    (live: a stray {"kind": "smoke"} would have taken about 48 minutes). A release that fails
    is logged and the message still never acked."""
    sqs = client("sqs")
    bodies = [
        "not json",
        "[1, 2]",
        '{"kind": "smoke"}',
        json.dumps({"kind": "resume", "chat_id": "c1", "message_id": "m1"}),
        json.dumps({"kind": "start", "chat_id": "c1", "message_id": "m1", "extra": 1}),
    ]
    with Stubber(sqs) as stub, caplog.at_level(logging.WARNING, logger="contextforge.infra.aws"):
        for i, body in enumerate(bodies):
            message = {"MessageId": f"id{i}", "ReceiptHandle": f"receipt-{i}", "Body": body}
            stub.add_response("receive_message", {"Messages": [message]}, receive_params())
            release = {
                "QueueUrl": QUEUE_URL,
                "ReceiptHandle": f"receipt-{i}",
                "VisibilityTimeout": 0,
            }
            if i == len(bodies) - 1:
                stub.add_client_error(
                    "change_message_visibility",
                    service_error_code="AccessDenied",
                    expected_params=release,
                )
            else:
                stub.add_response("change_message_visibility", {}, release)
        # No delete_message is stubbed: one would fail the stub.
        assert [SqsQueue(sqs, QUEUE_URL).receive() for _ in bodies] == [None] * len(bodies)
        stub.assert_no_pending_responses()
    messages = [r.getMessage() for r in caplog.records]
    assert messages[:-1] == [
        f"Malformed work message id{i} left for the DLQ" for i in range(len(bodies))
    ]
    assert "Could not release malformed message id4 (AccessDenied)" in messages[-1]


def test_ack_after_the_receipt_expired_raises_queue_error() -> None:
    sqs = client("sqs")
    with Stubber(sqs) as stub:
        stub.add_client_error(
            "delete_message",
            service_error_code="InvalidParameterValue",
            service_message="Reason: The receipt handle has expired.",
            expected_params={"QueueUrl": QUEUE_URL, "ReceiptHandle": "stale"},
        )
        with pytest.raises(QueueError, match="receipt expired"):
            SqsQueue(sqs, QUEUE_URL).ack(
                WorkMessage(kind="start", chat_id="c1", message_id="m1", receipt="stale")
            )
