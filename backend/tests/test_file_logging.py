import json
import logging

from contextforge.core.logging import JsonFormatter, configure, request_context


def test_file_logging_is_live_contextual_redacted_and_not_duplicated(tmp_path) -> None:
    root = logging.getLogger()
    original = root.handlers[:]
    original_level = root.level
    path = tmp_path / "backend.log"
    try:
        configure(path, secrets=('test-secret-"value',))
        configure(path, secrets=('test-secret-"value',))
        with request_context("request-1", "task-2"):
            logging.getLogger("contextforge.file-test").warning('token=test-secret-"value')
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["request_message_id"] == "request-1"
        assert records[0]["task_id"] == "task-2"
        assert records[0]["message"] == "token=[redacted]"
        file_handler = next(
            h for h in root.handlers if getattr(h, "baseFilename", None) == str(path)
        )
        assert file_handler.maxBytes == 10_000_000 and file_handler.backupCount == 3
    finally:
        for handler in root.handlers[:]:
            if handler not in original:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(original_level)


def test_log_exception_redaction_keeps_a_valid_json_record() -> None:
    formatter = JsonFormatter(("secret-in-error",))
    try:
        raise ValueError("secret-in-error")
    except ValueError:
        import sys

        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", None, sys.exc_info()
        )
    data = json.loads(formatter.format(record))
    assert "secret-in-error" not in data["exception"]
    assert "ValueError: [redacted]" in data["exception"]
