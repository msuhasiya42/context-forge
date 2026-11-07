"""ModelClient decorator that records one ModelCall per provider call."""

import hashlib
import json
from collections.abc import Mapping

from contextforge.core.context.prompts import prompt_version
from contextforge.core.entities.model_call import CallMetadata, ModelCall, Outcome
from contextforge.core.ids import new_ulid, now_iso_ms
from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse, ToolSpec
from contextforge.core.models.errors import ModelError, ModelTimeout, ModelUnreachable
from contextforge.core.models.transcript import TranscriptWriter, transcript
from contextforge.core.repositories.base import ModelCallRepository
from contextforge.core.values import Usage

# config/models.yaml prices: model id -> price kind (inputPerMTok, ...) -> USD per million tokens.
PriceTable = Mapping[str, Mapping[str, float]]
# The usage of a call that never reached the provider: known, and zero.
_NOT_SENT = Usage(input_tokens=0, output_tokens=0)


def price_row(prices: PriceTable, model: str) -> Mapping[str, float] | None:
    """The prices of ``model``. A geo inference-profile id ``<geo>.<model id>`` (for example
    ``apac.amazon.nova-pro-v1:0``) is billed at its model's row."""
    return prices.get(model) or prices.get(model.partition(".")[2])


def cost_usd(prices: PriceTable, model: str, usage: Usage | None) -> float | None:
    """What ``usage`` costs at ``model``'s prices; None when the usage is unknown or the model has
    no price. Cache writes without a price of their own are charged as input."""
    row = price_row(prices, model)
    if usage is None or row is None:
        return None
    per_million = (
        usage.input_tokens * row["inputPerMTok"]
        + usage.output_tokens * row["outputPerMTok"]
        + (usage.cache_read_tokens or 0) * row["cacheReadPerMTok"]
        + (usage.cache_write_tokens or 0) * row.get("cacheWritePerMTok", row["inputPerMTok"])
    )
    return per_million / 1_000_000


def worst_case_usd(prices: PriceTable, request: ModelRequest, input_tokens: int) -> float | None:
    """The most ``request`` can cost: ``input_tokens`` (the context cap) in and its
    ``max_tokens`` out at its model's prices; None when the model has no price."""
    usage = Usage(input_tokens=input_tokens, output_tokens=request.max_tokens)
    return cost_usd(prices, request.model, usage)


def tool_set_hash(tools: list[ToolSpec]) -> str | None:
    """sha256 of the canonical JSON of the specs sorted by name; None when no tools were sent.

    Sorting makes the hash name the tool set, not the order the caller listed it in.
    """
    if not tools:
        return None
    specs = [spec.model_dump(by_alias=True) for spec in sorted(tools, key=lambda s: s.name)]
    canonical = json.dumps(specs, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RecordingModelClient:
    """Wraps a ``ModelClient`` for one request and saves a ModelCall for every call it makes.

    A failed call is saved with outcome ERROR (TIMEOUT for a timeout) and the exception
    re-raised. A replayed response is not a provider call, so it is returned without a record.

    ``estimated_cost_usd`` is priced from the reported usage and ``prices``, so it can be
    recomputed later; a call whose usage is unknown (a timeout, an error) or whose model has no
    price is charged ``unknown_cost_usd``, since unknown is never zero. A call that never
    reached the provider (``ModelUnreachable``) used no tokens, so it is charged nothing.
    ``model`` is recorded as ``<provider>/<model id>`` (spec 3.5). With ``transcripts``, each
    record also gets its call's transcript (the exact request and the response or error).
    """

    def __init__(
        self,
        inner: ModelClient,
        model_calls: ModelCallRepository,
        *,
        request_message_id: str,
        memory_version: str,
        price_version: str,
        prices: PriceTable | None = None,
        unknown_cost_usd: float | None = None,
        provider: str = "bedrock",
        transcripts: TranscriptWriter | None = None,
    ) -> None:
        self._inner = inner
        self._model_calls = model_calls
        self._request_message_id = request_message_id
        self._memory_version = memory_version
        self._price_version = price_version
        self._prices: PriceTable = prices or {}
        self._unknown_cost_usd = unknown_cost_usd
        self._provider = provider
        self._transcripts = transcripts

    def invoke(self, request: ModelRequest) -> ModelResponse:
        # Id and start time are taken before the call so records sort by when calls began.
        request = request.model_copy(update={"request_message_id": self._request_message_id})
        model_call_id = new_ulid()
        started_at = now_iso_ms()
        try:
            response = self._inner.invoke(request)
        except Exception as exc:
            self._model_calls.save(
                self._record(
                    request,
                    model_call_id,
                    started_at,
                    outcome=Outcome.TIMEOUT if isinstance(exc, ModelTimeout) else Outcome.ERROR,
                    usage=_NOT_SENT if isinstance(exc, ModelUnreachable) else None,
                    failure_reason=str(exc),
                    langsmith_run_id=exc.langsmith_run_id if isinstance(exc, ModelError) else None,
                )
            )
            raise
        if response.replayed:
            return response
        self._model_calls.save(
            self._record(
                request,
                model_call_id,
                started_at,
                outcome=Outcome.COMPLETED,
                usage=response.usage,
                response=response,
                langsmith_run_id=response.langsmith_run_id,
            )
        )
        return response.model_copy(update={"model_call_id": model_call_id})

    def _record(
        self,
        request: ModelRequest,
        model_call_id: str,
        started_at: str,
        *,
        outcome: Outcome,
        usage: Usage | None,
        response: ModelResponse | None = None,
        failure_reason: str | None = None,
        langsmith_run_id: str | None = None,
    ) -> ModelCall:
        # Taken first, so the latency never includes the transcript write.
        ended_at = None if outcome == Outcome.TIMEOUT else now_iso_ms()
        context_version = f"mem:{self._memory_version}|prompt:{prompt_version(request.stage)}"
        # A call never sent costs nothing, whether or not its model has a price.
        cost = 0.0 if usage is _NOT_SENT else cost_usd(self._prices, request.model, usage)
        model = f"{self._provider}/{request.model}"
        return ModelCall(
            model_call_id=model_call_id,
            request_message_id=self._request_message_id,
            task_id=request.task_id,
            stage=request.stage,
            model=model,
            context_version=context_version,
            usage=usage,
            price_version=self._price_version,
            estimated_cost_usd=cost if cost is not None else self._unknown_cost_usd,
            stop_reason=response.stop_reason if response is not None else None,
            tool_set_hash=tool_set_hash(request.tools),
            langsmith_run_id=langsmith_run_id,
            transcript_ref=self._transcript(
                request, model_call_id, model, response=response, error=failure_reason
            ),
            outcome=outcome,
            failure_reason=failure_reason,
            metadata=_metadata(request, response),
            started_at=started_at,
            ended_at=ended_at,
        )

    def _transcript(
        self,
        request: ModelRequest,
        model_call_id: str,
        model: str,
        *,
        response: ModelResponse | None,
        error: str | None,
    ) -> str | None:
        """Store the call's transcript; its ref, or None when transcripts are off."""
        if self._transcripts is None:
            return None
        record = transcript(request, model, response=response, error=error)
        return self._transcripts.write(self._request_message_id, model_call_id, record)


def _metadata(request: ModelRequest, response: ModelResponse | None) -> CallMetadata:
    """What the adapter reported about the answer, and the stage's model when a fallback
    served the call in its place."""
    metadata = dict(response.metadata) if response is not None else {}
    if request.requested_model is not None:
        metadata["requested_model"] = request.requested_model
    return metadata
