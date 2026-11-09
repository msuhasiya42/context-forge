"""DynamoDB INSERT -> SQS reference envelope. Duplicate delivery is expected."""
import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
sqs = boto3.client("sqs", config=Config(
    connect_timeout=2, read_timeout=2,
    retries={"mode": "standard", "total_max_attempts": 2},
))


def handler(event, context):
    for record in event.get("Records", []):
        # Defense in depth: status changes and TTL deletes must not enqueue work.
        if record.get("eventName") != "INSERT":
            continue
        image = record["dynamodb"].get("NewImage", {})
        if image.get("status", {}).get("S") != "QUEUED":
            continue
        sequence = record["dynamodb"]["SequenceNumber"]
        try:
            job_id = image["jobId"]["S"]
            version = int(image["jobVersion"]["N"])
            if not job_id or version < 1:
                raise ValueError("invalid job identity")
            message = {
                "schemaVersion": 1,
                "type": "research.job.requested",
                "eventId": record["eventID"],
                "jobId": job_id,
                "jobVersion": version,
                "idempotencyKey": f"{job_id}:{version}",
                "sourceRegion": record["awsRegion"],
            }
            response = sqs.send_message(
                QueueUrl=os.environ["QUEUE_URL"],
                MessageBody=json.dumps(message, separators=(",", ":")),
            )
            logger.info(json.dumps({"eventId": record["eventID"],
                                    "messageId": response["MessageId"]}))
        except Exception as error:
            # Do not log prompts, full records, or exception payloads.
            logger.error("Forward failed: %s", type(error).__name__)
            # Stop here: stream checkpoints resume from this sequence number.
            return {"batchItemFailures": [{"itemIdentifier": sequence}]}
    return {"batchItemFailures": []}
