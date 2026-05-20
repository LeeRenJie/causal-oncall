"""TDD spec for PhoenixTracer.

W3-S4 upgrade: real Arize Phoenix SDK instrumentation. The OTLP exporter
is faked at the recorder seam (``FakePhoenixClient``) so the tests never
open a network socket and never write JSONL to disk. We exercise the
full public surface — ``traced`` decorator, ``record_outcome``, and
``accuracy_dashboard_data`` — plus the internal seams that gate
behavior (stdout fallback, outcome-store persistence, rolling-window
filtering, trend bucketing).
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from causal_oncall.phoenix_tracer import (
    AccuracyDashboardData,
    PhoenixTracer,
    PhoenixTracerConfig,
    _bucket_trend,
    _OutcomeStore,
    _parse_ts,
    _StdoutSpanRecorder,
)
from tests.fakes import FakePhoenixClient


def _cfg(**overrides) -> PhoenixTracerConfig:
    base = {
        "collector_endpoint": "",  # empty → stdout fallback (unless tests inject recorder)
        "project_name": "causal-oncall-test",
        "outcome_store_path": Path("/tmp/_unused_for_injected_store.jsonl"),
        "rolling_window_days": 30,
        "trend_buckets": 6,
    }
    base.update(overrides)
    return PhoenixTracerConfig(**base)


def _fixed_clock(when: datetime):
    return lambda: when


# ---------------------------------------------------------------------------
# traced() decorator
# ---------------------------------------------------------------------------


def test_traced_decorator_creates_span_around_call():
    fake = FakePhoenixClient()
    tracer = PhoenixTracer(_cfg(), recorder=fake, outcome_store=fake)

    @tracer.traced("triage")
    def fn(x: int) -> int:
        return x + 1

    assert fn(1) == 2
    assert len(fake.spans) == 1
    assert fake.spans[0]["name"] == "triage"
    assert fake.spans[0]["attributes"]["agent.name"] == "triage"
    assert fake.spans[0]["attributes"]["openinference.span.kind"] == "AGENT"
    assert fake.spans[0]["ended"] is True
    assert fake.spans[0]["error"] is None


def test_traced_decorator_records_exceptions_on_the_span():
    fake = FakePhoenixClient()
    tracer = PhoenixTracer(_cfg(), recorder=fake, outcome_store=fake)

    @tracer.traced("triage")
    def boom():
        raise RuntimeError("nope")

    with contextlib.suppress(RuntimeError):
        boom()

    assert fake.spans[0]["ended"] is True
    assert isinstance(fake.spans[0]["error"], RuntimeError)


def test_traced_decorator_preserves_function_metadata():
    """@wraps means the wrapped function keeps its name + doc — important
    for the Phoenix UI rendering of the trace tree."""
    fake = FakePhoenixClient()
    tracer = PhoenixTracer(_cfg(), recorder=fake, outcome_store=fake)

    @tracer.traced("triage")
    def my_handler(x: int) -> int:
        """do the thing."""
        return x

    assert my_handler.__name__ == "my_handler"
    assert "do the thing" in (my_handler.__doc__ or "")


# ---------------------------------------------------------------------------
# record_outcome()
# ---------------------------------------------------------------------------


def test_record_outcome_writes_eval_row_to_outcome_store():
    fake = FakePhoenixClient()
    tracer = PhoenixTracer(_cfg(), recorder=fake, outcome_store=fake)

    tracer.record_outcome("span-7", top_hypothesis_correct=True)

    assert len(fake.outcomes) == 1
    row = fake.outcomes[0]
    assert row["span_id"] == "span-7"
    assert row["top_hypothesis_correct"] is True
    assert row["score"] == 1.0
    assert row["label"] == "top_hypothesis_correct"
    assert row["project"] == "causal-oncall-test"
    assert "recorded_at" in row


def test_record_outcome_also_annotates_the_span():
    """Outcome write goes to BOTH the store (for our dashboard) AND the
    span (so Phoenix's UI shows the eval inline with the trace)."""
    fake = FakePhoenixClient()
    tracer = PhoenixTracer(_cfg(), recorder=fake, outcome_store=fake)

    tracer.record_outcome("span-7", top_hypothesis_correct=False)

    assert fake.span_annotations["span-7"] == [False]
    assert fake.outcomes[0]["score"] == 0.0
    assert fake.outcomes[0]["top_hypothesis_correct"] is False


def test_record_outcome_uses_injected_clock():
    fake = FakePhoenixClient()
    pinned = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    tracer = PhoenixTracer(_cfg(), recorder=fake, outcome_store=fake, clock=_fixed_clock(pinned))

    tracer.record_outcome("span-1", top_hypothesis_correct=True)
    assert fake.outcomes[0]["recorded_at"] == pinned.isoformat()


# ---------------------------------------------------------------------------
# accuracy_dashboard_data()
# ---------------------------------------------------------------------------


def test_accuracy_dashboard_data_on_empty_store_returns_zeroes():
    """Cold-start: no eval rows yet → 0.0 accuracy, empty trend filled with 0s."""
    fake = FakePhoenixClient()
    tracer = PhoenixTracer(_cfg(trend_buckets=4), recorder=fake, outcome_store=fake)

    data = tracer.accuracy_dashboard_data()

    assert isinstance(data, AccuracyDashboardData)
    assert data.rolling_accuracy == 0.0
    assert data.total_briefs == 0
    assert data.confirmed_count == 0
    assert data.trend == (0.0, 0.0, 0.0, 0.0)


def test_accuracy_dashboard_data_computes_rolling_accuracy():
    fake = FakePhoenixClient()
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    # 7 in-window outcomes: 5 correct, 2 wrong = 5/7 ≈ 0.714
    for i in range(5):
        fake.seed_outcome(
            span_id=f"good-{i}",
            top_hypothesis_correct=True,
            recorded_at=now - timedelta(days=i),
        )
    for i in range(2):
        fake.seed_outcome(
            span_id=f"bad-{i}",
            top_hypothesis_correct=False,
            recorded_at=now - timedelta(days=i + 5),
        )
    tracer = PhoenixTracer(_cfg(), recorder=fake, outcome_store=fake, clock=_fixed_clock(now))

    data = tracer.accuracy_dashboard_data()

    assert data.total_briefs == 7
    assert data.confirmed_count == 5
    assert data.rolling_accuracy == pytest.approx(5 / 7)


def test_accuracy_dashboard_data_excludes_rows_outside_window():
    """Eval rows older than the rolling window get filtered out."""
    fake = FakePhoenixClient()
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    # In-window: 1 correct
    fake.seed_outcome(
        span_id="recent", top_hypothesis_correct=True, recorded_at=now - timedelta(days=5)
    )
    # Out-of-window: 99 wrong — should NOT drag the metric down
    for i in range(99):
        fake.seed_outcome(
            span_id=f"ancient-{i}",
            top_hypothesis_correct=False,
            recorded_at=now - timedelta(days=60 + i),
        )
    tracer = PhoenixTracer(
        _cfg(rolling_window_days=30),
        recorder=fake,
        outcome_store=fake,
        clock=_fixed_clock(now),
    )

    data = tracer.accuracy_dashboard_data()

    assert data.total_briefs == 1
    assert data.confirmed_count == 1
    assert data.rolling_accuracy == 1.0


def test_accuracy_dashboard_data_trend_has_configured_bucket_count():
    fake = FakePhoenixClient()
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    # One row in each of 3 buckets across a 30-day window with 6 buckets.
    # Buckets are 5 days each — drop one correct + one wrong in distinct buckets.
    fake.seed_outcome(span_id="a", top_hypothesis_correct=True, recorded_at=now - timedelta(days=2))
    fake.seed_outcome(
        span_id="b", top_hypothesis_correct=False, recorded_at=now - timedelta(days=12)
    )
    fake.seed_outcome(
        span_id="c", top_hypothesis_correct=True, recorded_at=now - timedelta(days=22)
    )
    tracer = PhoenixTracer(
        _cfg(rolling_window_days=30, trend_buckets=6),
        recorder=fake,
        outcome_store=fake,
        clock=_fixed_clock(now),
    )

    data = tracer.accuracy_dashboard_data()

    assert len(data.trend) == 6
    # The last bucket should be 1.0 (the recent correct one)
    assert data.trend[-1] == 1.0


def test_accuracy_dashboard_data_simulated_six_month_curve():
    """UNIQUE_IDEA wow #4 path: simulate accuracy climbing 41% -> 73%.

    Pre-seed an early bucket with ~41% accuracy and a later bucket with
    ~73% accuracy, run dashboard query, assert the trend captures the
    climb.
    """
    fake = FakePhoenixClient()
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    # Old bucket (oldest day in window): 41% correct (7 of 17)
    for i in range(7):
        fake.seed_outcome(
            span_id=f"old-good-{i}",
            top_hypothesis_correct=True,
            recorded_at=now - timedelta(days=29, hours=i),
        )
    for i in range(10):
        fake.seed_outcome(
            span_id=f"old-bad-{i}",
            top_hypothesis_correct=False,
            recorded_at=now - timedelta(days=29, hours=i + 8),
        )
    # New bucket (most recent day): 73% correct (8 of 11)
    for i in range(8):
        fake.seed_outcome(
            span_id=f"new-good-{i}",
            top_hypothesis_correct=True,
            recorded_at=now - timedelta(hours=i),
        )
    for i in range(3):
        fake.seed_outcome(
            span_id=f"new-bad-{i}",
            top_hypothesis_correct=False,
            recorded_at=now - timedelta(hours=i + 9),
        )
    tracer = PhoenixTracer(
        _cfg(rolling_window_days=30, trend_buckets=6),
        recorder=fake,
        outcome_store=fake,
        clock=_fixed_clock(now),
    )

    data = tracer.accuracy_dashboard_data()

    # First bucket (oldest) ≈ 0.41; last bucket (newest) ≈ 0.73
    assert data.trend[0] == pytest.approx(7 / 17, abs=0.01)
    assert data.trend[-1] == pytest.approx(8 / 11, abs=0.01)


# ---------------------------------------------------------------------------
# Stdout fallback recorder (the W1-style behavior, retained for dev parity).
# ---------------------------------------------------------------------------


def test_recorder_defaults_to_stdout_when_collector_endpoint_is_empty():
    """Empty collector endpoint → ``_StdoutSpanRecorder`` selected."""
    tracer = PhoenixTracer(_cfg(collector_endpoint=""))
    # No injected recorder; the constructor should have chosen stdout.
    assert isinstance(tracer._recorder, _StdoutSpanRecorder)


def test_stdout_recorder_emits_lifecycle_lines(capsys):
    recorder = _StdoutSpanRecorder("test-project")
    span_id = recorder.start_span("triage", {"agent.name": "triage"})
    recorder.end_span(span_id, error=None)
    recorder.annotate_outcome(span_id, top_hypothesis_correct=True)
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3
    start_evt = json.loads(out[0])
    end_evt = json.loads(out[1])
    ann_evt = json.loads(out[2])
    assert start_evt["event"] == "span.start"
    assert start_evt["span_id"] == span_id
    assert start_evt["project"] == "test-project"
    assert end_evt["event"] == "span.end"
    assert end_evt["error"] is None
    assert ann_evt["event"] == "span.annotate"
    assert ann_evt["top_hypothesis_correct"] is True


def test_stdout_recorder_renders_error_repr_on_end_span(capsys):
    recorder = _StdoutSpanRecorder("test-project")
    span_id = recorder.start_span("triage", {})
    capsys.readouterr()  # drain start line
    recorder.end_span(span_id, error=RuntimeError("boom"))
    out = capsys.readouterr().out.splitlines()
    end_evt = json.loads(out[0])
    assert "RuntimeError" in end_evt["error"]


# ---------------------------------------------------------------------------
# Outcome-store persistence (real disk write).
# ---------------------------------------------------------------------------


def test_outcome_store_persists_rows_to_jsonl(tmp_path):
    store = _OutcomeStore(tmp_path / "outcomes.jsonl")
    store.append({"span_id": "a", "top_hypothesis_correct": True})
    store.append({"span_id": "b", "top_hypothesis_correct": False})

    rows = store.read_rows()
    assert len(rows) == 2
    assert rows[0]["span_id"] == "a"
    assert rows[1]["top_hypothesis_correct"] is False


def test_outcome_store_creates_parent_dir(tmp_path):
    """First write creates the parent directory — Cloud Run cold-start
    safety so the volume mount is initialized on demand."""
    nested = tmp_path / "deep" / "nested" / "outcomes.jsonl"
    store = _OutcomeStore(nested)
    store.append({"span_id": "a"})
    assert nested.exists()


def test_outcome_store_read_rows_returns_empty_when_file_missing(tmp_path):
    store = _OutcomeStore(tmp_path / "does-not-exist.jsonl")
    assert store.read_rows() == []


def test_outcome_store_skips_blank_lines(tmp_path):
    """Defensive — log rotators sometimes leave trailing blank lines."""
    path = tmp_path / "outcomes.jsonl"
    path.write_text('{"span_id":"a"}\n\n{"span_id":"b"}\n   \n', encoding="utf-8")
    store = _OutcomeStore(path)
    assert [r["span_id"] for r in store.read_rows()] == ["a", "b"]


def test_outcome_store_caches_after_first_read(tmp_path):
    """Second read hits the cache, not the file — keeps the dashboard
    cheap when the page is refreshed."""
    path = tmp_path / "outcomes.jsonl"
    store = _OutcomeStore(path)
    store.append({"span_id": "a"})
    store.read_rows()
    # Mutate the file directly; cached read should still return old data
    path.write_text('{"span_id":"new"}\n', encoding="utf-8")
    assert [r["span_id"] for r in store.read_rows()] == ["a"]


def test_outcome_store_append_invalidates_cache(tmp_path):
    """A subsequent append must invalidate the cache so the new row
    shows up on the next read."""
    path = tmp_path / "outcomes.jsonl"
    store = _OutcomeStore(path)
    store.append({"span_id": "a"})
    store.read_rows()  # prime cache
    store.append({"span_id": "b"})
    rows = store.read_rows()
    assert [r["span_id"] for r in rows] == ["a", "b"]


def test_phoenix_tracer_default_outcome_store_persists_to_configured_path(tmp_path):
    """End-to-end: record_outcome with no injected store writes to disk
    at the configured path."""
    fake_recorder = FakePhoenixClient()
    outcome_path = tmp_path / "outcomes.jsonl"
    tracer = PhoenixTracer(
        _cfg(outcome_store_path=outcome_path),
        recorder=fake_recorder,
        # outcome_store=None → real _OutcomeStore at the configured path
    )
    tracer.record_outcome("span-1", top_hypothesis_correct=True)

    assert outcome_path.exists()
    line = outcome_path.read_text(encoding="utf-8").strip()
    row = json.loads(line)
    assert row["span_id"] == "span-1"
    assert row["top_hypothesis_correct"] is True


# ---------------------------------------------------------------------------
# Pure-helper coverage — _parse_ts, _bucket_trend.
# ---------------------------------------------------------------------------


def test_parse_ts_handles_missing_timestamp():
    """Legacy rows without a recorded_at field default to "ancient"."""
    parsed = _parse_ts({"span_id": "legacy"})
    assert parsed == datetime.min.replace(tzinfo=UTC)


def test_parse_ts_handles_naive_iso_string():
    """A naive ISO string gets coerced to UTC so window math is total."""
    parsed = _parse_ts({"recorded_at": "2026-05-20T12:00:00"})
    assert parsed.tzinfo == UTC
    assert parsed.year == 2026 and parsed.hour == 12


def test_parse_ts_passes_through_aware_timestamp():
    parsed = _parse_ts({"recorded_at": "2026-05-20T12:00:00+00:00"})
    assert parsed.tzinfo == UTC


def test_bucket_trend_returns_empty_when_buckets_zero():
    """Degenerate config — no trend requested."""
    assert _bucket_trend([], datetime.now(UTC), datetime.now(UTC), 0) == ()


def test_bucket_trend_returns_flat_zero_when_window_collapsed():
    """If window_end == window_start the bin width is 0 — gracefully
    return a zero baseline instead of dividing by zero."""
    now = datetime(2026, 5, 20, tzinfo=UTC)
    trend = _bucket_trend([], now, now, 4)
    assert trend == (0.0, 0.0, 0.0, 0.0)


def test_bucket_trend_clamps_future_rows_into_last_bucket():
    """A row at exactly the window_end lands in the last bucket
    (right-edge inclusive) rather than overflowing past the array."""
    now = datetime(2026, 5, 20, tzinfo=UTC)
    window_start = now - timedelta(days=10)
    rows = [
        {
            "recorded_at": now.isoformat(),
            "top_hypothesis_correct": True,
        }
    ]
    trend = _bucket_trend(rows, window_start, now, 5)
    assert trend[-1] == 1.0
    assert trend[0] == 0.0


def test_bucket_trend_drops_rows_before_window_start():
    """Belt-and-braces: rows earlier than window_start return idx<0 and
    are skipped (not silently miscounted in bucket 0)."""
    now = datetime(2026, 5, 20, tzinfo=UTC)
    window_start = now - timedelta(days=10)
    rows = [
        {
            "recorded_at": (window_start - timedelta(days=1)).isoformat(),
            "top_hypothesis_correct": True,
        }
    ]
    trend = _bucket_trend(rows, window_start, now, 5)
    assert all(v == 0.0 for v in trend)
