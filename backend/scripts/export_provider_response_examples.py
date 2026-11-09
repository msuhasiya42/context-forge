"""Export provider-shaped fictional responses, not live MCP captures."""

import json
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]  # scripts/ -> backend/ -> repo root
FOLDER = REPO_ROOT / "docs/session-examples/login-incident/reference/response-formats"


def save(name: str, data: object) -> None:
    (FOLDER / name).write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    FOLDER.mkdir(parents=True, exist_ok=True)
    rows = [
        json.loads(line)
        for line in (FOLDER.parent / "06-full-metrics-artifact.jsonl").read_text().splitlines()
    ]
    series = []
    for region in dict.fromkeys(row["region"] for row in rows):
        for outcome in ["failure", "success"]:
            values = []
            for row in rows:
                if row["region"] != region:
                    continue
                end = datetime.fromisoformat(row["timestamp"]) + timedelta(minutes=1)
                count = row["failed_logins"]
                if outcome == "success":
                    count = row["total_logins"] - count
                values.append([int(end.timestamp()), str(count)])
            series.append({"metric": {"region": region, "outcome": outcome}, "values": values})
    # model.Matrix serializes as a list: Grafana's wrapper differs from Prometheus REST.
    payload = {"data": series}
    save("01-grafana-payload.json", payload)
    save(
        "02-grafana-mcp-envelope.json",
        {
            "jsonrpc": "2.0",
            "id": "metrics-001",
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
            },
        },
    )
    save("03-normalized-metrics-row.json", rows[300])
    document = (
        "Release: auth-2026.11.08.1\nService: auth-api\n\n"
        "Changes\nChanged token-validation retry handling.\n\n"
        "Operational notes\nWatch login failures by region after deployment.\n"
        "These release notes describe intended changes, not verified incident causes.\n"
    )
    drive = {"fileContent": document}
    save("04-drive-content-payload.json", drive)
    save(
        "05-drive-content-mcp-envelope.json",
        {
            "jsonrpc": "2.0",
            "id": "drive-read-001",
            "result": {
                "content": [{"type": "text", "text": json.dumps(drive)}],
                "structuredContent": drive,
            },
        },
    )
    (FOLDER / "06-release-notes-extracted.txt").write_text(document)
    save(
        "07-proposed-drive-artifact.json",
        {
            "provider": "Google Drive MCP",
            "tool": "read_file_content",
            "fileId": "demo-release-notes-1",
            "retrievedAt": "2026-11-08T06:00:00Z",
            "sourceUrl": "https://example.org/drive/release-notes-1",
            "sourceVersion": None,
            "complete": None,
            "response": drive,
        },
    )
    save(
        "08-jira-normalized-adapter-output.json",
        {
            "status": "ok",
            "rows": [
                {
                    "issueKey": "DEMO-41",
                    "issueType": "Bug",
                    "status": "Open",
                    "summary": "Investigate token-validation retry timeouts",
                    "descriptionText": "Possible timeout behavior; cause not confirmed.",
                    "url": "https://example.org/jira/DEMO-41",
                }
            ],
            "text": "One fictional relevant issue; duplicate analysis is a separate step.",
        },
    )
    # A short, attributed excerpt of the published Prometheus example, not an incident capture.
    save(
        "09-published-prometheus-sample-excerpt.json",
        {
            "metric": {"__name__": "up", "job": "prometheus", "instance": "localhost:9090"},
            "value": [1435781451.781, "1"],
        },
    )
    save(
        "10-drive-search-payload.json",
        {
            "files": [
                {
                    "id": "demo-release-notes-1",
                    "title": "auth-api release auth-2026.11.08.1",
                    "mimeType": "application/vnd.google-apps.document",
                    "viewUrl": "https://example.org/drive/release-notes-1",
                    "contentSnippet": "Changed token-validation retry handling.",
                }
            ],
        },
    )
    # Verify sample mapping without pretending this performs production counter handling.
    decoded = json.loads(
        json.loads((FOLDER / "02-grafana-mcp-envelope.json").read_text())["result"]["content"][0][
            "text"
        ]
    )
    assert decoded == payload
    assert len(series) == 10 and sum(len(s["values"]) for s in series) == 1200
    assert sum(float(v[1]) for s in series for v in s["values"]) == 600000
    assert (
        json.loads((FOLDER / "04-drive-content-payload.json").read_text())["fileContent"]
        == (FOLDER / "06-release-notes-extracted.txt").read_text()
    )
    print("Verified 10 series / 1,200 samples, JSON text round-trip and extracted document text.")


if __name__ == "__main__":
    main()
