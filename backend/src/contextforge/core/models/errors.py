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
    """The provider had a transient fault (5xx, a connection closed mid-call). The call may
    have reached it, so its usage is unknown."""

    retryable = True


class ModelUnreachable(ModelUnavailable):
    """No connection to the provider could be made (a connect timeout, an unreachable
    endpoint), so the call was never sent: it used no tokens and costs nothing."""


class ModelOverloaded(ModelUnavailable):
    """The provider answered that it cannot serve the call now (HTTP 503, Bedrock's
    ServiceUnavailableException): a refusal before generation, so it used no tokens."""


class ModelThrottled(ModelError):
    """The provider refused the call for rate; a later attempt may succeed."""

    retryable = True


class ModelAccessDenied(ModelError):
    """The account may not invoke the model ("not available for this account", a missing
    Marketplace subscription). Retrying will not help; another model may answer."""


class ModelRejected(ModelError):
    """The provider refused the content: a content filter or guardrail stopped the response."""


class ModelMalformed(ModelError):
    """The provider stopped on output it could not form (Nova's malformed_tool_use on a nested
    tool schema). The same call often succeeds on another try, so it is retried once."""

    retryable = True
