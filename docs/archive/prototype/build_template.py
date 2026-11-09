"""Build a dependency-free CloudFormation template with embedded Lambda code."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ref = lambda name: {"Ref": name}
get = lambda name, attr: {"Fn::GetAtt": [name, attr]}
sub = lambda value: {"Fn::Sub": value}
tags = [{"Key": "Project", "Value": "EnterpriseResearchAgent"},
        {"Key": "Environment", "Value": "learning"}]
resources = {
    "ResearchJob": {
        "Type": "AWS::DynamoDB::Table",
        "Properties": {
            "TableName": "ResearchJob",
            "BillingMode": "PROVISIONED", "TableClass": "STANDARD",
            "ProvisionedThroughput": {"ReadCapacityUnits": 1, "WriteCapacityUnits": 1},
            "AttributeDefinitions": [{"AttributeName": "jobId", "AttributeType": "S"}],
            "KeySchema": [{"AttributeName": "jobId", "KeyType": "HASH"}],
            "StreamSpecification": {"StreamViewType": "NEW_IMAGE"},
            "TimeToLiveSpecification": {"AttributeName": "expireAt", "Enabled": True},
            "SSESpecification": {"SSEEnabled": False},
            "Tags": tags,
        },
    },
    "JobQueue": {
        "Type": "AWS::SQS::Queue",
        "Properties": {"QueueName": "research-jobs-prototype",
                       "SqsManagedSseEnabled": True, "MessageRetentionPeriod": 86400,
                       "VisibilityTimeout": 60, "ReceiveMessageWaitTimeSeconds": 20,
                       "Tags": tags},
    },
    "StreamFailures": {
        "Type": "AWS::SQS::Queue",
        "Properties": {"QueueName": "research-stream-failures-prototype",
                       "SqsManagedSseEnabled": True, "MessageRetentionPeriod": 1209600,
                       "Tags": tags},
    },
    "BridgeLogGroup": {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {"LogGroupName": "/aws/lambda/research-job-stream-bridge",
                       "RetentionInDays": 3, "Tags": tags},
    },
    "BridgeRole": {
        "Type": "AWS::IAM::Role",
        "Properties": {
            "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
                "Action": "sts:AssumeRole"}]},
            "Policies": [{"PolicyName": "ReadOneStreamSendTwoQueuesWriteOwnLogs",
                          "PolicyDocument": {"Version": "2012-10-17", "Statement": [
                              {"Effect": "Allow", "Action": ["dynamodb:DescribeStream", "dynamodb:GetRecords", "dynamodb:GetShardIterator"], "Resource": get("ResearchJob", "StreamArn")},
                              {"Effect": "Allow", "Action": "dynamodb:ListStreams", "Resource": "*"},
                              {"Effect": "Allow", "Action": "sqs:SendMessage", "Resource": [get("JobQueue", "Arn"), get("StreamFailures", "Arn")]},
                              {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"], "Resource": get("BridgeLogGroup", "Arn")},
                          ]}}],
            "Tags": tags,
        },
    },
    "StreamBridge": {
        "Type": "AWS::Lambda::Function", "DependsOn": "BridgeLogGroup",
        "Properties": {
            "FunctionName": "research-job-stream-bridge", "Runtime": "python3.13",
            "Handler": "index.handler", "Role": get("BridgeRole", "Arn"),
            "MemorySize": 128, "Timeout": 10, "Architectures": ["arm64"],
            "Environment": {"Variables": {"QUEUE_URL": ref("JobQueue")}},
            "Code": {"ZipFile": (ROOT / "bridge.py").read_text()}, "Tags": tags,
        },
    },
    "StreamMapping": {
        "Type": "AWS::Lambda::EventSourceMapping",
        "Properties": {
            "EventSourceArn": get("ResearchJob", "StreamArn"),
            "FunctionName": ref("StreamBridge"), "Enabled": True,
            "StartingPosition": "TRIM_HORIZON", "BatchSize": 5,
            "MaximumBatchingWindowInSeconds": 0, "ParallelizationFactor": 1,
            "MaximumRetryAttempts": 3, "MaximumRecordAgeInSeconds": 3600,
            "BisectBatchOnFunctionError": True,
            "FunctionResponseTypes": ["ReportBatchItemFailures"],
            "FilterCriteria": {"Filters": [{"Pattern": json.dumps({
                "eventName": ["INSERT"], "dynamodb": {"NewImage": {"status": {"S": ["QUEUED"]}}}
            })}]},
            "DestinationConfig": {"OnFailure": {"Destination": get("StreamFailures", "Arn")}},
        },
    },
}
template = {
    "AWSTemplateFormatVersion": "2010-09-09",
    "Description": "Learning-only ResearchJob -> Streams -> Lambda -> SQS. No model calls or public endpoints.",
    "Resources": resources,
    "Outputs": {
        "TableName": {"Value": ref("ResearchJob")},
        "QueueUrl": {"Value": ref("JobQueue")},
        "FailureQueueUrl": {"Value": ref("StreamFailures")},
        "FunctionName": {"Value": ref("StreamBridge")},
        "StreamArn": {"Value": get("ResearchJob", "StreamArn")},
        "BridgeRoleArn": {"Value": get("BridgeRole", "Arn")},
        "MappingId": {"Value": ref("StreamMapping")},
    },
}
if __name__ == "__main__":
    output = ROOT / "prototype.template.json"
    output.write_text(json.dumps(template, indent=2) + "\n")
    print(output)
