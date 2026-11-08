"""Bundle reviewed teaching fixtures into a single presentation JSON file."""

import json
from pathlib import Path

from scripts.session_narrative import narrated

FOLDER = Path("docs/session-examples/login-incident")


def read(name: str) -> dict:
    return json.loads((FOLDER / "reference" / name).read_text())


def main() -> None:
    planner = read("01-planner-bedrock-request.json")
    plan = read("03-example-normalized-planner-response.json")
    request = json.loads(planner["messages"][0]["content"][0]["text"])
    first = read("09-executor-readback-request.json")
    continuation = read("11-next-executor-context.json")
    # Fixture commentary is not part of a model message.
    for message in first["messages"]:
        message.pop("status", None)
        message.pop("normalizationNote", None)
    next_request = {
        "status": "Proposed logical model request; fully expanded retained messages",
        "system": first["system"],
        "tools": first["tools"],
        "messages": first["messages"] + continuation["messagesToAppend"],
    }
    bundle = {
        "00_read_first": {
            "purpose": "60-minute login incident walkthrough; expand numbered stages in order",
            "truth": "Fictional incident and scripted model outputs; no live MCP/model/Jira calls",
            "implemented": "Existing process_rows generated full NDJSON and shortened preview",
            "proposed": "Private MCP adapters, artifact-query tool, incident tasks and Jira action",
            "formats": "Planner has Bedrock wire payload; executor messages are logical examples",
            "data_files": "Large original rows and provider payloads remain under reference/",
        },
        "01_request_and_planner": {
            "userRequestAndBackendContext": request,
            "fullBedrockRequest": planner,
            "planOutputSchema": read("02-proposed-plan-schema.json"),
            "scriptedPlan": plan,
            "illustrativePersistedTaskMapping": [
                {
                    "taskId": "demo-" + task["ref"],
                    "plannerRef": task["ref"],
                    "requestMessageId": "demo-request-001",
                    "status": "PLANNED",
                    "dependsOn": ["demo-" + dep for dep in task["dependsOn"]],
                    "note": "Teaching projection, not serialization accepted by current Task model",
                }
                for task in plan["tasks"]
            ],
        },
        "02_execute_and_receive_data": {
            "metricsAssistantCall": read("05-metrics-tool-call.json"),
            "fullProviderEnvelopeFile": "reference/response-formats/02-grafana-mcp-envelope.json",
            "normalizedRow": read("response-formats/03-normalized-metrics-row.json"),
            "driveCalls": read("15-drive-mcp-calls.json"),
            "driveSearchPayload": read("response-formats/10-drive-search-payload.json"),
            "driveReadEnvelope": read("response-formats/05-drive-content-mcp-envelope.json"),
            "jiraAdapterOutput": read("response-formats/08-jira-normalized-adapter-output.json"),
            "otherCompletedTasks": read("12-other-task-results.json"),
        },
        "03_store_and_shorten": {
            "artifactMetadata": read("07-artifact-record.json"),
            "fullRowsFile": "reference/06-full-metrics-artifact.jsonl",
            "actualPreviewMessage": read("08-shortened-tool-message.json"),
            "proposedDriveSnapshot": read("response-formats/07-proposed-drive-artifact.json"),
            "lesson": "First 50 rows show baseline only; they cannot establish the spike",
        },
        "04_read_back_and_continue": {
            "executorRequestWithPreview": first,
            "backendReadAndAggregate": read("10-backend-readback-and-result.json"),
            "fullNextExecutorRequest": next_request,
            "scriptedTaskResult": continuation["scriptedTaskResult"],
        },
        "05_synthesize_and_prepare_action": read("13-synthesis-and-proposed-jira-action.json"),
        "06_execute_action_and_answer": read("14-simulated-write-and-final-answer.json"),
    }
    # A ticket reader needs resolved source references, not local E1/E2/E3 names.
    action = bundle["05_synthesize_and_prepare_action"]["proposedAction"]
    action["arguments"]["description"] = (
        bundle["05_synthesize_and_prepare_action"]["summary"]
        + "\nMetrics: demo-login-metrics-001; attach authorized aggregate export before execution."
        + "\nRelated issue: https://example.org/jira/DEMO-41"
        + "\nRelease notes: https://example.org/drive/release-notes-1"
    )
    action["backendChecks"].append("Resolve metrics evidence into an authorized attachment/link")
    # This file stays a teaching trace, never a replayable job submission.
    (FOLDER / "SESSION.json").write_text(json.dumps(narrated(bundle), indent=2) + "\n")
    assert len(next_request["messages"]) == 5
    assert next_request["messages"][-1]["tool_call_id"] == "artifact-query-001"
    assert next_request["messages"][-2]["tool_calls"][0]["id"] == "artifact-query-001"
    print("Bundled five narrated model calls with full input/output and Python actions.")


if __name__ == "__main__":
    main()
