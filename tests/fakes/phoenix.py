"""In-process fakes for the Phoenix recorder + outcome store seams.

These let unit tests exercise the full ``PhoenixTracer`` public surface
without touching the OTLP exporter or the filesystem JSONL store. The
fakes mirror the same protocols the real ``_OtelSpanRecorder`` and
``_OutcomeStore`` satisfy.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


class FakePhoenixClient:
    """Deep fake combining the recorder + outcome-store seams.

    Used as a single injection point: tests pass the same instance as
    both ``recorder`` and ``outcome_store`` to ``PhoenixTracer``. The
    in-memory ``spans`` + ``outcomes`` lists let the tests assert on
    every observable side effect without going through OTLP or disk.
    """

    def __init__(self) -> None:
        # span lifecycle log — one record per started span.
        self.spans: list[dict[str, Any]] = []
        # outcome rows — same schema the real JSONL outcome store writes.
        self.outcomes: list[dict[str, Any]] = []
        # span_id -> outcomes annotated against it (for ordering assertions).
        self.span_annotations: dict[str, list[bool]] = {}
        self._counter = 0

    # ---- recorder protocol ---------------------------------------------- #

    def start_span(self, name: str, attributes: dict[str, Any]) -> str:
        self._counter += 1
        span_id = f"fake-span-{self._counter}"
        self.spans.append(
            {
                "span_id": span_id,
                "name": name,
                "attributes": dict(attributes),
                "ended": False,
                "error": None,
            }
        )
        return span_id

    def end_span(self, span_id: str, *, error: BaseException | None = None) -> None:
        for record in self.spans:
            if record["span_id"] == span_id:
                record["ended"] = True
                record["error"] = error
                return

    def annotate_outcome(self, span_id: str, *, top_hypothesis_correct: bool) -> None:
        self.span_annotations.setdefault(span_id, []).append(top_hypothesis_correct)

    # ---- outcome-store protocol ----------------------------------------- #

    def append(self, row: dict[str, Any]) -> None:
        self.outcomes.append(dict(row))

    def read_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.outcomes]

    # ---- helpers for arranging test fixtures ---------------------------- #

    def seed_outcome(
        self,
        *,
        span_id: str,
        top_hypothesis_correct: bool,
        recorded_at: datetime | None = None,
        project: str = "causal-oncall-test",
    ) -> None:
        """Pre-populate an outcome row as if a prior run recorded it.

        Useful for the rolling-accuracy tests where the dashboard reads
        from an existing eval-row corpus.
        """
        ts = recorded_at if recorded_at is not None else datetime.now(UTC)
        self.outcomes.append(
            {
                "span_id": span_id,
                "label": "top_hypothesis_correct",
                "score": 1.0 if top_hypothesis_correct else 0.0,
                "top_hypothesis_correct": top_hypothesis_correct,
                "project": project,
                "recorded_at": ts.isoformat(),
            }
        )
