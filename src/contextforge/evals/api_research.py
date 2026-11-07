"""Submit real research through the running API and save inspectable progress and evidence.

Run creates normal chats/messages visible to the same user in the UI. Watch uses GET only.
Neither command starts another backend or changes its model, spend or tool limits.
"""

import argparse
import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from contextforge.core.settings import Settings
from contextforge.evals.web_research import capture_run

BUILD = "web-research-v3"
FINISHED = {"COMPLETED", "FAILED", "CANCELLED", "WAITING_USER"}


def _save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def submit_run(
    client: httpx.Client,
    question: str,
    out_dir: Path,
    *,
    chat_id: str | None = None,
    title: str | None = None,
    idempotency_key: str | None = None,
) -> tuple[str, str]:
    """Check the build before any writes; persist the submission identity before POSTing."""
    if not question.strip():
        raise ValueError("The research question cannot be blank.")
    health = _json(client.get("/health"))
    if health.get("build") != BUILD:
        raise ValueError(f"Restart the API first: expected build {BUILD}.")
    if (out_dir / "run.json").exists():
        raise ValueError("This output folder already contains a run; use watch or a new folder.")
    metadata = {
        "chatId": chat_id,
        "requestId": None,
        "idempotencyKey": idempotency_key or str(uuid.uuid4()),
        "health": health,
    }
    _save(out_dir / "run.json", metadata)
    if chat_id is None:
        chat = _json(
            client.post(
                "/chats", json={"title": title or "API: " + " ".join(question.split())[:60]}
            )
        )
        chat_id = chat["chatId"]
        metadata["chatId"] = chat_id
        _save(out_dir / "run.json", metadata)
    request = _json(
        client.post(
            f"/chats/{chat_id}/requests",
            json={"content": question},
            headers={"Idempotency-Key": str(metadata["idempotencyKey"])},
        )
    )
    request_id: str = request["messageId"]
    metadata["requestId"] = request_id
    _save(out_dir / "run.json", metadata)
    return chat_id, request_id


def watch_run(
    client: httpx.Client,
    chat_id: str,
    request_id: str,
    out_dir: Path,
    *,
    poll_seconds: float = 2,
    timeout_seconds: float = 960,
    case_id: str = "api-research",
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> int:
    """Poll one existing request. Timeout/interrupt do not cancel or resubmit its work."""
    if poll_seconds <= 0 or timeout_seconds <= 0:
        raise ValueError("Polling interval and timeout must be positive.")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = f"/chats/{chat_id}/requests/{request_id}"
    started = time.monotonic()
    status = "ACCEPTED"
    previous = ""
    with (out_dir / "progress.jsonl").open("a", encoding="utf-8") as progress:
        while True:
            detail = _json(client.get(path))
            _save(out_dir / "detail.json", detail)
            status = detail["request"]["status"]
            event = {
                "status": status,
                "chatId": chat_id,
                "requestId": request_id,
                "tasks": [
                    {"taskId": task["taskId"], "kind": task["kind"], "status": task["status"]}
                    for task in detail["tasks"]
                ],
                "webToolCalls": detail["request"].get("webToolCalls"),
                "usage": detail.get("usage", {}),
            }
            signature = json.dumps(event, sort_keys=True)
            if signature != previous:
                event["elapsedSeconds"] = round(time.monotonic() - started, 1)
                progress.write(json.dumps(event) + "\n")
                progress.flush()
                if emit:
                    emit(event)
                previous = signature
            if status in FINISHED or time.monotonic() - started >= timeout_seconds:
                break
            time.sleep(poll_seconds)
    trace = _json(client.get(path + "/trace"))
    _save(out_dir / "trace.json", trace)
    if status not in FINISHED:
        _save(out_dir / "result.json", {"status": "WATCH_TIMEOUT", "requestStatus": status})
        return 2
    try:
        bundle = capture_run(client, chat_id, request_id, case_id)
    except (httpx.HTTPError, ValueError) as exc:
        _save(
            out_dir / "result.json",
            {"status": status, "captureError": type(exc).__name__, "detail": str(exc)},
        )
        return 2 if status == "WAITING_USER" else 1
    _save(out_dir / "bundle.json", bundle)
    report = bundle["report"]
    text = "\n\n".join(bundle["deliverables"]) or report["content"]
    (out_dir / "report.md").write_text(text + "\n", encoding="utf-8")
    findings = len(report["report"]["claims"])
    _save(out_dir / "result.json", {"status": status, "findings": findings})
    return 0 if status == "COMPLETED" and findings else 2 if status == "WAITING_USER" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Submit research; this can incur model/search charges.")
    run.add_argument("question")
    run.add_argument("--chat", help="Use an existing chat; default: create an API: chat.")
    run.add_argument("--title")
    run.add_argument("--idempotency-key")
    watch = commands.add_parser("watch", help="Watch an existing request without submitting work.")
    watch.add_argument("--chat", required=True)
    watch.add_argument("--request", required=True)
    for command in (run, watch):
        command.add_argument("--out-dir", type=Path)
        command.add_argument("--poll-seconds", type=float, default=2)
        command.add_argument("--timeout-seconds", type=float, default=960)
        command.add_argument(
            "--case", default="api-research", help="Benchmark case ID for capture."
        )
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0 or args.timeout_seconds <= 0:
        parser.error("Polling interval and timeout must be positive.")
    out_dir = args.out_dir or Path(".data/runs") / str(uuid.uuid4())
    settings = Settings()
    headers = (
        {"Authorization": f"Bearer {settings.dev_token.get_secret_value()}"}
        if settings.dev_token
        else {}
    )
    print(f"Run files: {out_dir}", flush=True)
    try:
        with httpx.Client(base_url=args.base_url, headers=headers, timeout=30) as client:
            if args.command == "run":
                chat_id, request_id = submit_run(
                    client,
                    args.question,
                    out_dir,
                    chat_id=args.chat,
                    title=args.title,
                    idempotency_key=args.idempotency_key,
                )
            else:
                chat_id, request_id = args.chat, args.request
            print(f"chat={chat_id} request={request_id}", flush=True)
            code = watch_run(
                client,
                chat_id,
                request_id,
                out_dir,
                poll_seconds=args.poll_seconds,
                timeout_seconds=args.timeout_seconds,
                case_id=args.case,
                emit=lambda event: print(json.dumps(event), flush=True),
            )
            print(f"Saved run files; exit={code}. A completed run still needs quality review.")
            return code
    except (httpx.HTTPError, ValueError) as exc:
        _save(out_dir / "client-error.json", {"type": type(exc).__name__, "message": str(exc)})
        print(f"API test stopped: {type(exc).__name__}: {exc}. See {out_dir} for saved progress.")
        return 1
    except KeyboardInterrupt:
        print("Stopped watching. Submitted work continues; use watch with the saved run IDs.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
