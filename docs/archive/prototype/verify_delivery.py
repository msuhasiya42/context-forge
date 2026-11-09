"""A small, bounded live smoke test. Only synthetic records; no model calls.

Run in AWS CloudShell after reviewing and deploying the stack.
Receives but does NOT delete SQS messages; test records expire via DynamoDB TTL.
"""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone

import boto3

parser = argparse.ArgumentParser()
parser.add_argument("--account-id", required=True)
parser.add_argument("--region", default="ap-southeast-2", choices=["ap-southeast-2"])
parser.add_argument("--stack", default="research-agent-prototype")
args = parser.parse_args()
session = boto3.Session(region_name=args.region)
account = session.client("sts").get_caller_identity()["Account"]
assert account == args.account_id, "Wrong AWS account; no test writes made."
stack = session.client("cloudformation").describe_stacks(StackName=args.stack)["Stacks"][0]
assert stack["StackStatus"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE"), stack["StackStatus"]
out = {o["OutputKey"]: o["OutputValue"] for o in stack["Outputs"]}
lam = session.client("lambda")
mapping = lam.get_event_source_mapping(UUID=out["MappingId"])
assert mapping["State"] == "Enabled", mapping["State"]
assert mapping["FunctionResponseTypes"] == ["ReportBatchItemFailures"]
function = lam.get_function_configuration(FunctionName=out["FunctionName"])
assert function["Role"] == out["BridgeRoleArn"], "Unexpected function execution role"
ddb = session.client("dynamodb")
sqs = session.client("sqs")
suffix = uuid.uuid4().hex[:12]
job_id, ignored_id = "smoke-" + suffix, "ignored-" + suffix
now = int(time.time())
for identity, status in ((job_id, "QUEUED"), (ignored_id, "RUNNING")):
    ddb.put_item(TableName=out["TableName"], ConditionExpression="attribute_not_exists(jobId)", Item={
        "jobId": {"S": identity}, "jobVersion": {"N": "1"}, "status": {"S": status},
        "prompt": {"S": "Synthetic delivery test; do not call a model."},
        "createdAt": {"S": datetime.now(timezone.utc).isoformat()},
        "expireAt": {"N": str(now + 86400)},
    })
# A status update must not create another logical job request.
ddb.update_item(TableName=out["TableName"], Key={"jobId": {"S": job_id}},
                UpdateExpression="SET #status = :status", ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": {"S": "RUNNING"}})
seen = []
deadline = time.monotonic() + 60
while time.monotonic() < deadline:
    response = sqs.receive_message(QueueUrl=out["QueueUrl"], WaitTimeSeconds=10,
                                  MaxNumberOfMessages=10, VisibilityTimeout=5)
    for message in response.get("Messages", []):
        body = json.loads(message["Body"])
        assert body.get("jobId") != ignored_id, "Non-QUEUED insert incorrectly forwarded"
        if body.get("jobId") == job_id:
            assert body["idempotencyKey"] == f"{job_id}:1"
            assert body["sourceRegion"] == args.region
            assert body["schemaVersion"] == 1
            assert "prompt" not in body
            seen.append({"messageId": message["MessageId"], "body": body})
    if seen:
        break
assert seen, "Timed out: no matching SQS message from the actual DynamoDB write"
result = {"verifiedAt": datetime.now(timezone.utc).isoformat(), "account": account,
          "region": args.region, "stack": args.stack, "mappingState": mapping["State"],
          "liveDelivery": "PASS", "jobId": job_id, "received": seen,
          "negativeTest": "No ignored-id seen during bounded observation; not a proof of perpetual absence.",
          "limits": "No real agent or LLM invoked; duplicate delivery remains possible."}
print(json.dumps(result, indent=2))
with open("delivery-result.json", "w") as output:
    json.dump(result, output, indent=2)
