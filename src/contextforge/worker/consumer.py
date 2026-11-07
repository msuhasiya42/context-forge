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
from contextforge.worker.dependencies import WorkerDependencies
from contextforge.worker.runner import RequestRunner

logger = logging.getLogger(__name__)

FAILED_RUN = "Research could not finish. Check provider access or retry as a new request."


class QueueConsumer:
    """Runs queued starts and resumes on one background thread; ``start`` after ``stop`` runs a
    new thread."""

    def __init__(self, worker_dependencies: WorkerDependencies, runner: RequestRunner) -> None:
        self.worker_dependencies, self.runner = worker_dependencies, runner
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def handle(self, work: WorkMessage) -> None:
        """Run one delivery, then ack it, unless it failed before the request was claimed: that
        request is still ACCEPTED, so the delivery stays on the queue to be redelivered. A run
        that fails after its claim moves the request to FAILED."""
        try:
            self._dispatch(work)
        except Exception:
            logger.exception("request %s failed", work.message_id)
            if self._unclaimed(work):
                logger.warning(
                    "request %s is still ACCEPTED; not acked, so it is redelivered", work.message_id
                )
                return
            self._fail_running(work)
        finally:
            self._touch_chat(work)
        self.worker_dependencies.queue.ack(work)

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                work = self.worker_dependencies.queue.receive()
                if work is not None:
                    self.handle(work)
                else:
                    self._stop.wait(0.1)
            except Exception:
                logger.exception("Queue delivery failed; retaining work for retry.")
                self._stop.wait(1)

    def start(self) -> None:
        """Start the consumer thread; a no-op while one is running."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="contextforge-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _dispatch(self, work: WorkMessage) -> None:
        if work.kind == "start":
            self.runner.start(work.chat_id, work.message_id)
            return
        reply = (
            self.worker_dependencies.repositories.messages.get(work.chat_id, work.reply_message_id)
            if work.reply_message_id
            else None
        )
        if not isinstance(reply, ClarificationResponseMessage):
            logger.warning(
                "resume of request %s names %s, which is not a clarification answer; dropped",
                work.message_id,
                work.reply_message_id,
            )
            return
        self.runner.resume_claimed(work.chat_id, work.message_id, reply)

    def _unclaimed(self, work: WorkMessage) -> bool:
        """True for a start whose request is still ACCEPTED: the run failed before its claim."""
        request = self.worker_dependencies.repositories.messages.get(work.chat_id, work.message_id)
        return (
            work.kind == "start"
            and isinstance(request, RequestMessage)
            and request.status == RequestStatus.ACCEPTED
        )

    def _fail_running(self, work: WorkMessage) -> None:
        request = self.worker_dependencies.repositories.messages.get(work.chat_id, work.message_id)
        if not isinstance(request, RequestMessage) or request.status != RequestStatus.RUNNING:
            return
        try:
            self.worker_dependencies.repositories.messages.update_request_status(
                work.chat_id,
                work.message_id,
                RequestStatus.RUNNING,
                RequestStatus.FAILED,
                FAILED_RUN,
            )
        except StaleStatusError as stale:
            logger.info(
                "request %s became %s meanwhile; left as it is", work.message_id, stale.actual
            )

    def _touch_chat(self, work: WorkMessage) -> None:
        request = self.worker_dependencies.repositories.messages.get(work.chat_id, work.message_id)
        if isinstance(request, RequestMessage):
            self.worker_dependencies.touch_chat(work.chat_id, request.user_id)
