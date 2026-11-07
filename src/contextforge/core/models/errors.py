"""Provider-neutral failures. ``retryable`` marks the transient ones; only those are retried."""

from typing import ClassVar


class ModelError(Exception):
    """A model call failed; retrying the same call will not help unless ``retryable``."""

    retryable: ClassVar[bool] = False
    # The LangSmith run of the failed call when it was traced, so its ModelCall links to it.
    langsmith_run_id: str | None = None


class ModelTimeout(ModelError):
    """No answer in time. The provider may still bill the call, so its usage is unknown."""

    retryable = True


class ModelUnavailable(ModelError):
    """The provider could not be reached or had a transient fault (5xx, connection closed)."""

    retryable = True


class ModelThrottled(ModelError):
    """The provider refused the call for rate; a later attempt may succeed."""

    retryable = True


class ModelRejected(ModelError):
    """The provider refused the content: a content filter or guardrail stopped the response."""
