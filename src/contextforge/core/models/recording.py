"""ModelClient decorator that records one ModelCall per provider call."""

import hashlib
import json

from contextforge.core.context.prompts import prompt_version
from contextforge.core.entities.model_call import ModelCall, Outcome
from contextforge.core.ids import new_ulid, now_iso
from contextforge.core.models.base import ModelClient, ModelRequest, ModelResponse, ToolSpec
from contextforge.core.models.errors import ModelTimeout
from contextforge.core.repositories.base import ModelCallRepository


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

    A failed call is saved with outcome ERROR and the exception re-raised. A replayed response
    is not a provider call, so it is returned without a record.
    """

    def __init__(
        self,
        inner: ModelClient,
        model_calls: ModelCallRepository,
        *,
        request_message_id: str,
        memory_version: str,
        price_version: str,
    ) -> None:
        self._inner = inner
        self._model_calls = model_calls
        self._request_message_id = request_message_id
        self._memory_version = memory_version
        self._price_version = price_version
        self.reserve_usd: float | None = None

    def invoke(self, request: ModelRequest) -> ModelResponse:
        # Id and start time are taken before the call so records sort by when calls began.
        request = request.model_copy(update={"request_message_id": self._request_message_id})
        model_call_id = new_ulid()
        started_at = now_iso()
        try:
            response = self._inner.invoke(request)
        except Exception as exc:
            self._model_calls.save(
                self._record(
                    request,
                    model_call_id,
                    started_at,
                    outcome=Outcome.TIMEOUT if isinstance(exc, ModelTimeout) else Outcome.ERROR,
                    failure_reason=str(exc),
                )
            )
            raise
        if not response.replayed:
            self._model_calls.save(
                self._record(
                    request, model_call_id, started_at, outcome=Outcome.COMPLETED, response=response
                )
            )
        return response

    def _record(
        self,
        request: ModelRequest,
        model_call_id: str,
        started_at: str,
        *,
        outcome: Outcome,
        response: ModelResponse | None = None,
        failure_reason: str | None = None,
    ) -> ModelCall:
        context_version = f"mem:{self._memory_version}|prompt:{prompt_version(request.stage)}"
        return ModelCall(
            model_call_id=model_call_id,
            request_message_id=self._request_message_id,
            task_id=request.task_id,
            stage=request.stage,
            model=request.model,
            context_version=context_version,
            usage=response.usage if response is not None else None,
            price_version=self._price_version,
            estimated_cost_usd=self.reserve_usd,
            stop_reason=response.stop_reason if response is not None else None,
            tool_set_hash=tool_set_hash(request.tools),
            langsmith_run_id=response.langsmith_run_id if response else None,
            outcome=outcome,
            failure_reason=failure_reason,
            started_at=started_at,
            ended_at=None if outcome == Outcome.TIMEOUT else now_iso(),
        )
