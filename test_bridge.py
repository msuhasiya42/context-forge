"""Focused tests for the delivery failure boundary; no AWS calls or dependencies."""
import copy
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.send_message.return_value = {"MessageId": "message-1"}
        modules = {"boto3": types.SimpleNamespace(client=lambda *a, **k: self.client),
                   "botocore": types.ModuleType("botocore"),
                   "botocore.config": types.SimpleNamespace(Config=lambda **k: k)}
        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location("bridge", Path(__file__).with_name("bridge.py"))
            self.bridge = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.bridge)
        self.env = patch.dict(os.environ, {"QUEUE_URL": "https://sqs.example/queue"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self, seq="1"):
        return {"eventName": "INSERT", "eventID": "event-" + seq,
                "awsRegion": "ap-southeast-2", "dynamodb": {
                    "SequenceNumber": seq, "NewImage": {"jobId": {"S": "job-1"},
                    "jobVersion": {"N": "1"}, "status": {"S": "QUEUED"},
                    "prompt": {"S": "PRIVATE PROMPT"}}}}

    def test_envelope_excludes_prompt_and_replay_has_same_idempotency_key(self):
        event = {"Records": [self.record()]}
        for _ in range(2):
            self.assertEqual(self.bridge.handler(event, None), {"batchItemFailures": []})
        bodies = [json.loads(call.kwargs["MessageBody"]) for call in self.client.send_message.call_args_list]
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(bodies[0]["idempotencyKey"], "job-1:1")
        self.assertNotIn("PRIVATE PROMPT", json.dumps(bodies))

    def test_failure_stops_batch_at_first_unsent_sequence(self):
        self.client.send_message.side_effect = [{"MessageId": "ok"}, RuntimeError("network")]
        result = self.bridge.handler({"Records": [self.record("1"), self.record("2"), self.record("3")]}, None)
        self.assertEqual(result, {"batchItemFailures": [{"itemIdentifier": "2"}]})
        self.assertEqual(self.client.send_message.call_count, 2)

    def test_updates_deletes_and_nonqueued_inserts_do_not_enqueue(self):
        records = [self.record() for _ in range(3)]
        records[0]["eventName"] = "MODIFY"
        records[1]["eventName"] = "REMOVE"
        records[2]["dynamodb"]["NewImage"]["status"]["S"] = "RUNNING"
        self.assertEqual(self.bridge.handler({"Records": records}, None), {"batchItemFailures": []})
        self.client.send_message.assert_not_called()

    def test_malformed_job_is_retried_and_not_silently_dropped(self):
        record = copy.deepcopy(self.record())
        del record["dynamodb"]["NewImage"]["jobVersion"]
        self.assertEqual(self.bridge.handler({"Records": [record]}, None),
                         {"batchItemFailures": [{"itemIdentifier": "1"}]})
        self.client.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
