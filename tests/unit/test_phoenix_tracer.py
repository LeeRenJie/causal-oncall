"""TDD spec for PhoenixTracer.

Phoenix's OTLP exporter is faked at the span-collector seam so the
test never opens a network socket. We are not testing the OTLP wire
protocol; we are testing that the right spans get created with the
right attributes.
"""

from __future__ import annotations

from causal_oncall.phoenix_tracer import PhoenixTracer, PhoenixTracerConfig


def _cfg() -> PhoenixTracerConfig:
    return PhoenixTracerConfig(
        collector_endpoint="http://localhost:6006",
        project_name="causal-oncall-test",
    )


class _SpanRecorder:
    def __init__(self) -> None:
        self.spans: list[dict] = []

    def start_span(self, name: str, attributes: dict) -> str:
        self.spans.append({"name": name, "attributes": dict(attributes), "ended": False})
        return f"span-{len(self.spans) - 1}"

    def end_span(self, span_id: str, *, error: BaseException | None = None) -> None:
        idx = int(span_id.split("-")[1])
        self.spans[idx]["ended"] = True
        self.spans[idx]["error"] = error


def test_traced_decorator_creates_span_around_call(monkeypatch):
    tracer = PhoenixTracer(_cfg())
    recorder = _SpanRecorder()
    monkeypatch.setattr(tracer, "_recorder", recorder, raising=False)

    @tracer.traced("triage")
    def fn(x: int) -> int:
        return x + 1

    assert fn(1) == 2
    assert len(recorder.spans) == 1
    assert recorder.spans[0]["attributes"].get("agent.name") == "triage"
    assert recorder.spans[0]["ended"] is True


def test_traced_decorator_records_exceptions_on_the_span(monkeypatch):
    tracer = PhoenixTracer(_cfg())
    recorder = _SpanRecorder()
    monkeypatch.setattr(tracer, "_recorder", recorder, raising=False)

    @tracer.traced("triage")
    def boom():
        raise RuntimeError("nope")

    import contextlib

    with contextlib.suppress(RuntimeError):
        boom()

    assert recorder.spans[0]["ended"] is True
    assert isinstance(recorder.spans[0]["error"], RuntimeError)


def test_record_outcome_writes_eval_row(monkeypatch):
    tracer = PhoenixTracer(_cfg())
    rows: list[dict] = []
    monkeypatch.setattr(tracer, "_eval_writer", lambda row: rows.append(row), raising=False)

    tracer.record_outcome("span-7", top_hypothesis_correct=True)
    assert len(rows) == 1
    assert rows[0]["span_id"] == "span-7"
    assert rows[0]["top_hypothesis_correct"] is True
