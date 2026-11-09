"""botocore failures: the AWS error code every adapter reads, and one mapping to the model
error classes for Bedrock.

The AWS error code is read only from a ``ClientError``. botocore's transport errors (a read
timeout, a closed connection) carry no service response, so they are mapped by type.
"""

from botocore.exceptions import (  # type: ignore[import-untyped]
    BotoCoreError,
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from contextforge.core.models.errors import (
    ModelAccessDenied,
    ModelError,
    ModelThrottled,
    ModelTimeout,
    ModelUnavailable,
    ModelUnreachable,
)

_THROTTLED = {"ThrottlingException", "TooManyRequestsException"}
_UNAVAILABLE = {"ServiceUnavailableException", "InternalServerException", "ModelNotReadyException"}
# Never retried: the request, the model id or the account's access has to change first.
_FIX_FIRST = {
    "ValidationException": "the request does not match what the model accepts",
    "AccessDeniedException": "check model access and the IAM policy for this model and region",
    "ResourceNotFoundException": "check the model or inference profile id and the region",
}


def error_code(exc: BaseException) -> str:
    """The AWS error code of a ``ClientError``; empty for any other exception.

    A ``BotoCoreError`` (a read timeout, a closed connection) carries no service response, so
    an adapter catches ``ClientError`` only and lets those propagate as themselves.
    """
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code", ""))
    return ""


def map_boto_error(exc: ClientError | BotoCoreError) -> ModelError:
    """The model error class for a botocore failure; its message never carries the payload."""
    if isinstance(exc, ClientError):
        return _client_error(exc)
    # No connection was made, so nothing was sent (review M6): retried and charged nothing.
    if isinstance(exc, ConnectTimeoutError | EndpointConnectionError):
        return ModelUnreachable(f"AWS could not be reached; check network connectivity: {exc}")
    if isinstance(exc, ReadTimeoutError):
        return ModelTimeout(f"AWS call timed out: {exc}")
    if isinstance(exc, ConnectionClosedError):
        return ModelUnavailable(f"AWS closed the connection before answering: {exc}")
    return ModelError(f"AWS call failed before reaching the service: {exc}")


def _client_error(exc: ClientError) -> ModelError:
    code = error_code(exc)
    message = exc.response.get("Error", {}).get("Message", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    detail = f"{exc.operation_name} failed with {code}: {message}"
    if code in _THROTTLED:
        return ModelThrottled(detail)
    if code == "ModelTimeoutException":
        return ModelTimeout(detail)
    if code in _UNAVAILABLE or status >= 500:
        return ModelUnavailable(detail)
    if code == "AccessDeniedException":
        return ModelAccessDenied(f"{detail} ({_FIX_FIRST[code]})")
    if hint := _FIX_FIRST.get(code):
        return ModelError(f"{detail} ({hint})")
    return ModelError(detail)
