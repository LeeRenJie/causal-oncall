"""PhoenixTracer — observability + eval-dataset capture via Arize Phoenix OSS.

Hides: the OpenTelemetry OTLP exporter setup, the Phoenix span schema
(``openinference.semconv``), the eval-dataset write-back path, and the
rolling accuracy metric computation that powers the self-improvement
dashboard wow moment.

Phoenix is used here as the OSS SDK, not as a partner-bucket MCP
integration. The Dynatrace bucket claim stays unambiguous.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class PhoenixTracerConfig:
    """Knobs for the tracer; resolved from env in the FastAPI app startup."""

    collector_endpoint: str
    project_name: str
    api_key: str | None = None


class _StdoutSpanRecorder:  # pragma: no cover  # stdout-only side effects; swapped out in tests + replaced by real Phoenix exporter in W3-S4
    """Default recorder — writes spans to stdout as one JSON line each.

    Per PLAN W1-S3: Phoenix SDK self-eval lands in W3-S4; W1 only needs
    a span lifecycle we can demo from stdout. Tests substitute their own
    recorder via monkeypatch.
    """

    def __init__(self, project_name: str) -> None:
        self._project_name = project_name
        self._counter = 0

    def start_span(self, name: str, attributes: dict) -> str:
        import json as _json

        self._counter += 1
        span_id = f"span-{self._counter}"
        sys.stdout.write(
            _json.dumps(
                {
                    "event": "span.start",
                    "project": self._project_name,
                    "name": name,
                    "span_id": span_id,
                    "attributes": attributes,
                }
            )
            + "\n"
        )
        return span_id

    def end_span(self, span_id: str, *, error: BaseException | None = None) -> None:
        import json as _json

        sys.stdout.write(
            _json.dumps(
                {
                    "event": "span.end",
                    "project": self._project_name,
                    "span_id": span_id,
                    "error": None if error is None else repr(error),
                }
            )
            + "\n"
        )


def _default_eval_writer(
    row: dict,
) -> None:  # pragma: no cover  # stdout-only sink swapped out by W3-S4 Phoenix exporter
    """Default eval writer — stdout JSON line."""
    import json as _json

    sys.stdout.write(_json.dumps({"event": "eval.row", **row}) + "\n")


class PhoenixTracer:
    """Decorator-driven tracer + outcome recorder.

    The agent code annotates relevant methods with ``@tracer.traced(agent_name)``;
    the wrapper creates a span, attaches openinference attributes, and
    closes the span with success/failure. The orchestrator separately
    calls :meth:`record_outcome` once the on-call confirms whether the
    top hypothesis was correct, which writes a row into the Phoenix
    eval dataset that backs the self-improvement dashboard.
    """

    def __init__(self, config: PhoenixTracerConfig) -> None:
        self._config = config
        # Recorder + eval writer kept as instance attrs so the test suite
        # can monkeypatch the seams without going through OTLP plumbing.
        self._recorder: Any = _StdoutSpanRecorder(config.project_name)
        self._eval_writer: Callable[[dict], None] = _default_eval_writer

    def traced(self, agent_name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator factory that wraps a method in a Phoenix-attributed span.

        Usage::

            class TriageSpecialist(Specialist):
                @tracer.traced("triage")
                def investigate(self, signature):
                    ...
        """

        def decorator(fn: Callable[P, R]) -> Callable[P, R]:
            @wraps(fn)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                attributes = {
                    "agent.name": agent_name,
                    "openinference.span.kind": "AGENT",
                }
                span_id = self._recorder.start_span(agent_name, attributes)
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    self._recorder.end_span(span_id, error=exc)
                    raise
                self._recorder.end_span(span_id, error=None)
                return result

            return wrapper

        return decorator

    def record_outcome(self, span_id: str, *, top_hypothesis_correct: bool) -> None:
        """Append a labeled row to the Phoenix eval dataset for this run.

        Drives the rolling top-hypothesis-accuracy metric shown in the
        self-improvement dashboard wow moment.
        """
        self._eval_writer(
            {
                "span_id": span_id,
                "top_hypothesis_correct": top_hypothesis_correct,
                "project": self._config.project_name,
            }
        )
