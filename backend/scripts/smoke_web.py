"""Opt-in real web/model smoke test through the HTTP API without binding a socket."""

import argparse
import json
import time
import uuid

from fastapi.testclient import TestClient

from contextforge.api.main import create_app
from contextforge.core.settings import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live", action="store_true", help="Allow real search and billable Bedrock calls."
    )
    parser.add_argument(
        "--question",
        default=(
            "Use official Python documentation to explain two notable changes "
            "in Python 3.13, with sources."
        ),
    )
    args = parser.parse_args()
    if not args.live:
        parser.error("Use --live to explicitly allow external search and model calls.")
    settings = Settings()
    headers = (
        {"Authorization": f"Bearer {settings.dev_token.get_secret_value()}"}
        if settings.dev_token
        else {}
    )
    with TestClient(create_app(settings), headers=headers) as client:
        chat_response = client.post("/chats", json={})
        chat_response.raise_for_status()
        chat_id = chat_response.json()["chatId"]
        response = client.post(
            f"/chats/{chat_id}/requests",
            json={"content": args.question},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        response.raise_for_status()
        request_id = response.json()["messageId"]
        path = f"/chats/{chat_id}/requests/{request_id}"
        deadline = time.monotonic() + settings.request_deadline_seconds + 10
        detail = {}
        while time.monotonic() < deadline:
            response = client.get(path)
            response.raise_for_status()
            detail = response.json()
            if detail["request"]["status"] not in {"ACCEPTED", "RUNNING"}:
                break
            time.sleep(0.5)
        messages_response = client.get(f"/chats/{chat_id}/messages?limit=100")
        messages_response.raise_for_status()
        messages = messages_response.json()["items"]
        report = next(
            (
                m
                for m in messages
                if m["kind"] == "report" and m.get("requestMessageId") == request_id
            ),
            None,
        )
        print(json.dumps({"detail": detail, "report": report}, indent=2))
        if (
            detail.get("request", {}).get("status") != "COMPLETED"
            or not report
            or not report["report"]["claims"]
        ):
            raise SystemExit(
                "Live acceptance incomplete; inspect the request status/failure reason above."
            )


if __name__ == "__main__":
    main()
