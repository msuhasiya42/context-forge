import json
from io import BytesIO

import boto3
from botocore.stub import Stubber

from contextforge.core.entities.chat import Chat
from contextforge.infra.aws import DynamoChats, DynamoStore, S3Blobs, SqsQueue


def client(service):
    return boto3.client(
        service,
        region_name="ap-southeast-2",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


def test_dynamodb_chat_shape_read_and_atomic_status_touch():
    db = client("dynamodb")
    store = DynamoStore(db, "contextforge")
    repos = DynamoChats(store)
    raw = {
        "chatId": "c1",
        "userId": "u1",
        "title": "Research",
        "status": "ACTIVE",
        "createdAt": "t",
        "updatedAt": "t",
        "lastRequestStatus": None,
    }
    item = store.item("USER#u1", "CHAT#c1", raw)
    with Stubber(db) as stub:
        stub.add_response("put_item", {}, {"TableName": "contextforge", "Item": item})
        stub.add_response(
            "get_item",
            {"Item": item},
            {
                "TableName": "contextforge",
                "Key": {"PK": {"S": "USER#u1"}, "SK": {"S": "CHAT#c1"}},
                "ConsistentRead": True,
            },
        )
        repos.save(Chat.model_validate(raw))
        assert repos.get("u1", "c1").title == "Research"


def test_s3_bytes_and_sqs_receipt_round_trip():
    s3 = client("s3")
    blobs = S3Blobs(s3, "test-bucket")
    with Stubber(s3) as stub:
        stub.add_response(
            "put_object", {}, {"Bucket": "test-bucket", "Key": "raw_result/c1/a1", "Body": b"hello"}
        )
        stub.add_response(
            "get_object",
            {"Body": BytesIO(b"hello")},
            {"Bucket": "test-bucket", "Key": "raw_result/c1/a1"},
        )
        assert blobs.put("raw_result/c1/a1", b"hello") == "s3://test-bucket/raw_result/c1/a1"
        assert blobs.get("s3://test-bucket/raw_result/c1/a1") == b"hello"
    sqs = client("sqs")
    queue = SqsQueue(sqs, "https://sqs.ap-southeast-2.amazonaws.com/123/work")
    with Stubber(sqs) as stub:
        stub.add_response(
            "receive_message",
            {
                "Messages": [
                    {
                        "ReceiptHandle": "receipt",
                        "Body": json.dumps({"kind": "start", "chat_id": "c1", "message_id": "r1"}),
                    }
                ]
            },
        )
        stub.add_response("delete_message", {})
        work = queue.receive()
        assert work.message_id == "r1" and work.receipt == "receipt"
        queue.ack(work)


def test_plan_is_one_dynamodb_transaction_and_conditional_conflicts_are_exposed():
    import pytest

    from contextforge.core.entities.task import Task
    from contextforge.core.repositories.base import StaleStatusError
    from contextforge.infra.aws import DynamoMessages, DynamoTasks

    db = client("dynamodb")
    store = DynamoStore(db, "contextforge")
    tasks = [
        Task(
            task_id=t,
            request_message_id="r1",
            kind="web_search",
            goal="Research",
            input_artifact_ids=[],
            status="PLANNED",
            attempt=1,
            created_at="t",
            updated_at="t",
        )
        for t in ["t1", "t2"]
    ]
    expected = {
        "TransactItems": [
            {
                "Put": {
                    "TableName": "contextforge",
                    "Item": store.item(
                        "REQ#r1", "TASK#" + t.task_id, t.model_dump(mode="json", by_alias=True)
                    ),
                }
            }
            for t in tasks
        ]
    }
    with Stubber(db) as stub:
        stub.add_response("transact_write_items", {}, expected)
        DynamoTasks(store).save_all(tasks)
    with Stubber(db) as stub:
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        stub.add_response("get_item", {"Item": {"status": {"S": "CANCELLED"}}})
        with pytest.raises(StaleStatusError) as conflict:
            DynamoMessages(store).update_request_status("c1", "r1", "RUNNING", "COMPLETED")
        assert conflict.value.actual == "CANCELLED"
