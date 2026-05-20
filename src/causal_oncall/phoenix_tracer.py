"""PhoenixTracer — observability + eval-dataset capture via Arize Phoenix OSS.

Hides:
- Phoenix / OpenTelemetry tracer-provider setup (``phoenix.otel.register``).
- The OTLP exporter selection (cloud Phoenix collector vs in-process stdout
  fallback when ``PHOENIX_COLLECTOR_ENDPOINT`` is unset).
- OpenInference span attributes (``openinference.span.kind``, ``agent.name``).
- The eval-annotation write path — span events plus a persistent JSONL
  outcome store on disk so the rolling top-hypothesis-accuracy metric
  survives Cloud Run cold starts.
- The rolling-window accuracy computation that powers the self-improvement
  dashboard wow moment (W3-S5).

Phoenix is used here as the OSS SDK, not as a partner-bucket MCP
integration. The Dynatrace bucket claim stays unambiguous (see
UNIQUE_IDEA §"Partner bucket integrity").

Why local computation for ``accuracy_dashboard_data()``: the full
``arize-phoenix`` package exposes a ``phoenix.Client()`` query interface
over its eval store, but we ship ``arize-phoenix-otel`` (the lighter OTEL
collector-only variant) — query APIs live in the heavy variant which
pulls native deps that the hackathon's Windows + Cloud Run targets do
not need. We persist every recorded outcome to a small JSONL file under
``PHOENIX_OUTCOME_STORE_PATH`` (defaults to ``./out/phoenix_outcomes.jsonl``)
and compute the rolling metric in-process over it. The OTel spans still
flow to whatever Phoenix collector the env points at — so the trace UI
inside Phoenix sees them; we just don't re-pull eval rows back to compute
the headline number. The outcome store schema is forward-compatible with
the Phoenix native eval row shape (``span_id``, ``label``, ``score``).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class PhoenixTracerConfig:
    """Knobs for the tracer; resolved from env in the FastAPI app startup.

    Attributes:
        collector_endpoint: OTLP endpoint for the Phoenix collector.
            Empty string means "fall back to stdout" (W1 dev behavior).
        project_name: Phoenix project the spans belong to.
        api_key: Optional API key forwarded as the ``PHOENIX_API_KEY``
            header on OTLP requests (Arize Cloud requires it; local
            ``phoenix serve`` does not).
        outcome_store_path: JSONL file where eval rows are persisted so
            ``accuracy_dashboard_data()`` survives restarts.
        rolling_window_days: Width of the rolling-accuracy window (30d
            per UNIQUE_IDEA §"Wow factor #4").
        trend_buckets: How many sub-buckets to split the window into for
            the dashboard sparkline. 6 buckets over 30 days = ~5 days each.
    """

    collector_endpoint: str
    project_name: str
    api_key: str | None = None
    outcome_store_path: Path = field(default_factory=lambda: Path("./out/phoenix_outcomes.jsonl"))
    rolling_window_days: int = 30
    trend_buckets: int = 6


@dataclass(frozen=True, slots=True)
class AccuracyDashboardData:
    """Snapshot of the rolling top-hypothesis accuracy that powers W3-S5.

    Computed from the outcome store at read time — never cached, so the
    dashboard always reflects the latest human-confirmed feedback.

    Attributes:
        rolling_accuracy: ``sum(correct) / total`` over the rolling window.
            ``0.0`` when ``total_briefs == 0`` (rather than NaN, so the
            chart renders cleanly on a cold start).
        total_briefs: Eval rows in the rolling window.
        confirmed_count: Subset of ``total_briefs`` where the human said
            the top hypothesis was correct.
        trend: One accuracy value per bucket, oldest first. Length is
            always ``config.trend_buckets``; empty buckets contribute
            ``0.0`` for a flat baseline.
    """

    rolling_accuracy: float
    total_briefs: int
    confirmed_count: int
    trend: tuple[float, ...]


# ---------------------------------------------------------------------------
# Internal seams — recorder + outcome store + clock.
#
# The recorder talks to OpenTelemetry (or stdout fallback). The outcome
# store persists eval rows. Both are swappable so the test suite can use
# in-memory fakes without monkeypatching the OTLP exporter.
# ---------------------------------------------------------------------------


class _RecorderProtocol(Protocol):
    def start_span(self, name: str, attributes: dict[str, Any]) -> str: ...
    def end_span(self, span_id: str, *, error: BaseException | None = None) -> None: ...
    def annotate_outcome(self, span_id: str, *, top_hypothesis_correct: bool) -> None: ...


class _StdoutSpanRecorder:
    """Dev/fallback recorder — writes one JSON line per span lifecycle event.

    Active when ``PHOENIX_COLLECTOR_ENDPOINT`` is unset, which keeps
    local ``uvicorn`` runs observable without standing up a collector.
    Production (Cloud Run with ``PHOENIX_COLLECTOR_ENDPOINT`` set) uses
    :class:`_OtelSpanRecorder` instead.
    """

    def __init__(self, project_name: str) -> None:
        self._project_name = project_name
        self._counter = 0

    def start_span(self, name: str, attributes: dict[str, Any]) -> str:
        self._counter += 1
        span_id = f"local-span-{self._counter}"
        sys.stdout.write(
            json.dumps(
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
        sys.stdout.write(
            json.dumps(
                {
                    "event": "span.end",
                    "project": self._project_name,
                    "span_id": span_id,
                    "error": None if error is None else repr(error),
                }
            )
            + "\n"
        )

    def annotate_outcome(self, span_id: str, *, top_hypothesis_correct: bool) -> None:
        sys.stdout.write(
            json.dumps(
                {
                    "event": "span.annotate",
                    "project": self._project_name,
                    "span_id": span_id,
                    "top_hypothesis_correct": top_hypothesis_correct,
                }
            )
            + "\n"
        )


class _OtelSpanRecorder:  # pragma: no cover  # exercised only when an OTLP collector is reachable; tested via the FakePhoenixClient seam
    """Real recorder — pushes spans through ``phoenix.otel``'s TracerProvider.

    The TracerProvider is built once at construction (against the resolved
    endpoint/api-key) and re-used for every span. Span ids are the raw
    OTel hex ids stringified, so they round-trip cleanly into the eval
    annotation calls.
    """

    def __init__(self, config: PhoenixTracerConfig) -> None:
        from phoenix.otel import register

        kwargs: dict[str, Any] = {
            "endpoint": config.collector_endpoint,
            "project_name": config.project_name,
            # SimpleSpanProcessor — incident pipelines fire in bursts so
            # batching adds latency to the demo without throughput win.
            "batch": False,
            "set_global_tracer_provider": False,
            # Avoid Phoenix's stdout banner on import; the FastAPI logger
            # will surface startup state.
            "verbose": False,
        }
        if config.api_key:
            kwargs["api_key"] = config.api_key
        self._provider = register(**kwargs)
        self._tracer = self._provider.get_tracer("causal_oncall")
        # Active OTel spans keyed by our stringified span id, so
        # ``end_span`` / ``annotate_outcome`` can target the right span.
        self._open: dict[str, Any] = {}

    def start_span(self, name: str, attributes: dict[str, Any]) -> str:
        span = self._tracer.start_span(name=name, attributes=attributes)
        ctx = span.get_span_context()
        span_id = f"{ctx.trace_id:032x}:{ctx.span_id:016x}"
        self._open[span_id] = span
        return span_id

    def end_span(self, span_id: str, *, error: BaseException | None = None) -> None:
        span = self._open.pop(span_id, None)
        if span is None:
            return
        if error is not None:
            span.record_exception(error)
            from opentelemetry.trace import Status, StatusCode

            span.set_status(Status(StatusCode.ERROR, str(error)))
        span.end()

    def annotate_outcome(self, span_id: str, *, top_hypothesis_correct: bool) -> None:
        span = self._open.get(span_id)
        if span is None:
            # Span already ended — fine; the eval row still lands in the
            # outcome store and the Phoenix UI can correlate by id later.
            return
        span.add_event(
            name="eval.top_hypothesis_correct",
            attributes={
                "eval.label": "top_hypothesis_correct",
                "eval.score": 1.0 if top_hypothesis_correct else 0.0,
            },
        )


class _OutcomeStore:
    """JSONL-backed eval-row store.

    One row per ``record_outcome`` call. Append-only; reads scan the
    whole file (rolling-window length is the cap, and the hackathon's
    expected volume is <10k rows over the demo lifetime — well within
    a single linear scan).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        # Cache of rows loaded from disk so a fresh process picks up
        # historical evals on first ``read_rows()`` without re-reading
        # the file on every dashboard tick.
        self._cached: list[dict[str, Any]] | None = None

    def append(self, row: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        # Invalidate cache; next read picks up the new row.
        self._cached = None

    def read_rows(self) -> list[dict[str, Any]]:
        if self._cached is not None:
            return list(self._cached)
        if not self._path.exists():
            self._cached = []
            return []
        rows: list[dict[str, Any]] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
        self._cached = rows
        return list(rows)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Public surface — three methods, narrow on purpose.
# ---------------------------------------------------------------------------


class PhoenixTracer:
    """Decorator-driven tracer + outcome recorder + rolling-accuracy reader.

    Public surface is intentionally three methods:

    * :meth:`traced` — decorator factory wrapping any callable in a span.
    * :meth:`record_outcome` — append a human-confirmed eval row.
    * :meth:`accuracy_dashboard_data` — snapshot the rolling accuracy
      curve for the W3-S5 self-improvement dashboard.

    Everything else (Phoenix tracer-provider setup, OTLP exporter choice,
    span hierarchy, outcome-store persistence, trend bucketing) is
    hidden.
    """

    def __init__(
        self,
        config: PhoenixTracerConfig,
        *,
        recorder: _RecorderProtocol | None = None,
        outcome_store: _OutcomeStore | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._config = config
        # Recorder selection: explicit injection wins (tests); otherwise
        # presence of a collector endpoint flips into real Phoenix mode,
        # absence falls back to stdout (W1 dev parity).
        if recorder is not None:
            self._recorder = recorder
        elif config.collector_endpoint:
            self._recorder = _OtelSpanRecorder(
                config
            )  # pragma: no cover  # constructed only when an OTLP collector is reachable; covered via injected recorder in unit tests
        else:
            self._recorder = _StdoutSpanRecorder(config.project_name)
        self._outcomes = outcome_store or _OutcomeStore(config.outcome_store_path)
        self._clock = clock

    def traced(self, agent_name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
        """Decorator factory that wraps a callable in a Phoenix-attributed span.

        Usage::

            class TriageSpecialist(Specialist):
                @tracer.traced("triage")
                def investigate(self, signature):
                    ...

        The wrapper attaches OpenInference attributes (``agent.name``,
        ``openinference.span.kind=AGENT``) so the Phoenix UI groups
        the run under the right agent in the trace tree, then ends
        the span with the success/failure status of the call.
        """

        def decorator(fn: Callable[P, R]) -> Callable[P, R]:
            @wraps(fn)
            def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                attributes: dict[str, Any] = {
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
        """Append a labeled eval row + annotate the span with the outcome.

        The eval row drives the rolling top-hypothesis-accuracy metric
        shown in the self-improvement dashboard wow moment. Callers wire
        this up from the Slack feedback handler (W2-S3) and the
        Dynatrace event-stream resolution path (W2-S4 follow-up).
        """
        self._recorder.annotate_outcome(
            span_id,
            top_hypothesis_correct=top_hypothesis_correct,
        )
        self._outcomes.append(
            {
                "span_id": span_id,
                "label": "top_hypothesis_correct",
                "score": 1.0 if top_hypothesis_correct else 0.0,
                "top_hypothesis_correct": top_hypothesis_correct,
                "project": self._config.project_name,
                "recorded_at": self._clock().isoformat(),
            }
        )

    def accuracy_dashboard_data(self) -> AccuracyDashboardData:
        """Snapshot the rolling top-hypothesis accuracy for the dashboard.

        Reads from the local JSONL outcome store, filters to the
        configured rolling window (default 30 days), and bins the result
        into ``config.trend_buckets`` for the sparkline.
        """
        now = self._clock()
        window_start = now - timedelta(days=self._config.rolling_window_days)
        in_window = [r for r in self._outcomes.read_rows() if _parse_ts(r) >= window_start]
        total = len(in_window)
        confirmed = sum(1 for r in in_window if bool(r.get("top_hypothesis_correct")))
        accuracy = (confirmed / total) if total else 0.0
        trend = _bucket_trend(in_window, window_start, now, self._config.trend_buckets)
        return AccuracyDashboardData(
            rolling_accuracy=accuracy,
            total_briefs=total,
            confirmed_count=confirmed,
            trend=trend,
        )


# ---------------------------------------------------------------------------
# Pure helpers — kept module-private so they don't leak onto the public surface.
# ---------------------------------------------------------------------------


def _parse_ts(row: dict[str, Any]) -> datetime:
    ts = row.get("recorded_at")
    if not ts:
        # Defensive: an outcome row with no timestamp is treated as
        # ancient so it falls outside any rolling window. Belt-and-braces
        # for legacy rows that the W1 stub never wrote a timestamp on.
        return datetime.min.replace(tzinfo=UTC)
    parsed = datetime.fromisoformat(ts)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _bucket_trend(
    rows: Iterable[dict[str, Any]],
    window_start: datetime,
    window_end: datetime,
    buckets: int,
) -> tuple[float, ...]:
    """Bucket the eval rows into N equal-width time bins, return accuracy per bin.

    Empty bins contribute 0.0 so the chart line stays continuous instead
    of breaking into NaN gaps.
    """
    if buckets <= 0:
        return ()
    total_seconds = (window_end - window_start).total_seconds()
    if total_seconds <= 0:
        return tuple(0.0 for _ in range(buckets))
    bucket_seconds = total_seconds / buckets
    bin_totals = [0] * buckets
    bin_correct = [0] * buckets
    for row in rows:
        ts = _parse_ts(row)
        offset = (ts - window_start).total_seconds()
        idx = int(offset // bucket_seconds)
        # Right-edge inclusive — the most recent eval lands in the last bin.
        if idx >= buckets:
            idx = buckets - 1
        if idx < 0:
            continue
        bin_totals[idx] += 1
        if bool(row.get("top_hypothesis_correct")):
            bin_correct[idx] += 1
    return tuple((bin_correct[i] / bin_totals[i]) if bin_totals[i] else 0.0 for i in range(buckets))


# ---------------------------------------------------------------------------
# Env-driven config factory — used by app.py + the CLI; pure plumbing.
# ---------------------------------------------------------------------------


def config_from_env() -> (
    PhoenixTracerConfig
):  # pragma: no cover  # thin env-shim; covered by manual smoke at app startup
    """Build a config from the standard ``PHOENIX_*`` env vars.

    Mirrors the env contract documented in ``.env.example``:

    * ``PHOENIX_COLLECTOR_ENDPOINT`` — empty means stdout fallback.
    * ``PHOENIX_PROJECT_NAME`` — defaults to ``causal-oncall``.
    * ``PHOENIX_API_KEY`` — optional auth header for Arize Cloud.
    * ``PHOENIX_OUTCOME_STORE_PATH`` — JSONL eval-row store path.
    """
    return PhoenixTracerConfig(
        collector_endpoint=os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", ""),
        project_name=os.environ.get("PHOENIX_PROJECT_NAME", "causal-oncall"),
        api_key=os.environ.get("PHOENIX_API_KEY") or None,
        outcome_store_path=Path(
            os.environ.get("PHOENIX_OUTCOME_STORE_PATH", "./out/phoenix_outcomes.jsonl")
        ),
    )
