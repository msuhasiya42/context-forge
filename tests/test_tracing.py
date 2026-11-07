from contextlib import nullcontext

from contextforge.core.models.base import ModelRequest, ModelResponse
from contextforge.infra import tracing


def test_trace_failure_never_repeats_or_prevents_model_call(monkeypatch):
    calls = []

    class Model:
        def invoke(self, request):
            calls.append(request)
            return ModelResponse(text="answer")

    class BrokenTrace:
        def __enter__(self):
            raise RuntimeError("telemetry unavailable")

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(tracing, "Client", lambda **kwargs: object())
    monkeypatch.setattr(tracing, "tracing_context", lambda **kwargs: nullcontext())
    monkeypatch.setattr(tracing, "trace", lambda **kwargs: BrokenTrace())
    model = tracing.TracedModel(Model(), "test", "project")
    request = ModelRequest(stage="execute", model="nova", system=[], messages=[])
    assert model.invoke(request).text == "answer"
    assert len(calls) == 1
