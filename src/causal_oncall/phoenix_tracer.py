"""PhoenixTracer — observability + eval-dataset capture via Arize Phoenix OSS.

Hides: the OpenTelemetry OTLP exporter setup, the Phoenix span schema
(``openinference.semconv``), the eval-dataset write-back path, and the
rolling accuracy metric computation that powers the self-improvement
dashboard wow moment.

Phoenix is used here as the OSS SDK, not as a partner-bucket MCP
integration. The Dynatrace bucket claim stays unambiguous.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class PhoenixTracerConfig:
    """Knobs for the tracer; resolved from env in the FastAPI app startup."""

    collector_endpoint: str
    project_name: str
    api_key: str | None = None


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

    def traced(self, agent_name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator factory that wraps a method in a Phoenix-attributed span.

        Usage::

            class TriageSpecialist(Specialist):
                @tracer.traced("triage")
                def investigate(self, signature):
                    ...
        """
        raise NotImplementedError(
            "Return a decorator that wraps the call in a Phoenix span tagged "
            "with agent_name and openinference semantic attributes."
        )

    def record_outcome(self, span_id: str, *, top_hypothesis_correct: bool) -> None:
        """Append a labeled row to the Phoenix eval dataset for this run.

        Drives the rolling top-hypothesis-accuracy metric shown in the
        self-improvement dashboard wow moment.
        """
        raise NotImplementedError(
            "Write a row to the Phoenix eval dataset linking span_id to "
            "the on-call's top_hypothesis_correct verdict."
        )
