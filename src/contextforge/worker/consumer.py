"""A single queue consumer. Checkpoints and conditional claims protect duplicate starts."""

import logging
import threading

from contextforge.core.entities.message import (
    ClarificationResponseMessage,
    RequestMessage,
    RequestStatus,
)
from contextforge.core.queue import WorkMessage
from contextforge.core.repositories.base import StaleStatusError
from contextforge.worker.deps import WorkerDeps
from contextforge.worker.runner import RequestRunner

logger = logging.getLogger(__name__)


class QueueConsumer:
    def __init__(self, deps: WorkerDeps, runner: RequestRunner) -> None:
        self.deps, self.runner = deps, runner
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, name="contextforge-worker", daemon=True)

    def handle(self, work: WorkMessage) -> None:
        try:
            if work.kind == "start":
                self.runner.start(work.chat_id, work.message_id)
            elif work.reply_message_id:
                reply = self.deps.repos.messages.get(work.chat_id, work.reply_message_id)
                if isinstance(reply, ClarificationResponseMessage):
                    self.runner.resume_claimed(work.chat_id, work.message_id, reply)
        except Exception:
            logger.exception("Request execution failed: %s", work.message_id)
            request = self.deps.repos.messages.get(work.chat_id, work.message_id)
            if isinstance(request, RequestMessage) and request.status == RequestStatus.RUNNING:
                try:
                    self.deps.repos.messages.update_request_status(
                        work.chat_id,
                        work.message_id,
                        RequestStatus.RUNNING,
                        RequestStatus.FAILED,
                        "Research could not finish. Check provider access "
                        "or retry as a new request.",
                    )
                except StaleStatusError:
                    pass
        finally:
            request = self.deps.repos.messages.get(work.chat_id, work.message_id)
            if isinstance(request, RequestMessage):
                self.deps.touch_chat(work.chat_id, request.user_id)
        self.deps.queue.ack(work)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                work = self.deps.queue.receive()
                if work is not None:
                    self.handle(work)
                else:
                    self.stop_event.wait(0.1)
            except Exception:
                logger.exception("Queue delivery failed; retaining work for retry.")
                self.stop_event.wait(1)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)
