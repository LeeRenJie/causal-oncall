"""TDD spec for the W3-S5 self-improvement dashboard data layer.

The FastAPI route handlers in ``app.py`` are excluded from the coverage
gate; the data + page-rendering helpers in ``dashboard.py`` are covered
here. Demo-mode handler, real-data adapter, and HTML serving are
exercised together so the route handlers reduce to one-liners that
delegate to these functions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from causal_oncall.dashboard import (
    DashboardPayload,
    _from_accuracy,
    dashboard_payload_from,
    demo_dashboard_payload,
    render_dashboard_page,
)
from causal_oncall.phoenix_tracer import (
    AccuracyDashboardData,
    PhoenixTracer,
    PhoenixTracerConfig,
)
from tests.fakes import FakePhoenixClient

# ---------------------------------------------------------------------------
# demo_dashboard_payload() -- hand-crafted curve for the 3-min demo
# ---------------------------------------------------------------------------


def test_demo_dashboard_payload_has_30_day_trend_climbing_to_73_percent():
    """Wow moment #4: 41% -> 73% curve over 30 days, lands cleanly on demo day."""
    payload = demo_dashboard_payload()

    assert isinstance(payload, DashboardPayload)
    assert payload.trend_length == 30
    assert len(payload.trend) == 30
    # Curve starts at the documented baseline (41%) and ends at the target (73%).
    assert payload.trend[0] == pytest.approx(0.41)
    assert payload.trend[-1] == pytest.approx(0.73)
    assert payload.starting_accuracy == pytest.approx(0.41)
    assert payload.rolling_accuracy == pytest.approx(0.73)


def test_demo_dashboard_payload_trend_is_monotonically_non_decreasing():
    """The wow moment is a rising curve -- no regressions in the canned data."""
    trend = demo_dashboard_payload().trend
    for prev, curr in pairwise(trend):
        assert curr >= prev, f"demo trend regressed: {prev} -> {curr}"


def test_demo_dashboard_payload_headline_counts_match_narration():
    """Subtitle says '147 briefs over 30 days, 107 human-confirmed' (~= 73%)."""
    payload = demo_dashboard_payload()
    assert payload.total_briefs == 147
    assert payload.confirmed_count == 107
    # Confirmed/total ratio matches the headline number to within rounding.
    assert payload.confirmed_count / payload.total_briefs == pytest.approx(0.728, abs=0.01)


def test_demo_dashboard_payload_to_dict_is_json_serializable_shape():
    """JSON contract for the page's fetch('/dashboard/data?demo=true') call."""
    body = demo_dashboard_payload().to_dict()
    assert set(body.keys()) == {
        "rolling_accuracy",
        "total_briefs",
        "confirmed_count",
        "trend",
        "starting_accuracy",
        "trend_length",
    }
    # ``trend`` must be a list (not tuple) so json.dumps round-trips cleanly.
    assert isinstance(body["trend"], list)
    assert body["trend"][0] == pytest.approx(0.41)
    assert body["trend"][-1] == pytest.approx(0.73)
    assert body["trend_length"] == 30


# ---------------------------------------------------------------------------
# dashboard_payload_from(tracer) -- real-data path
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> PhoenixTracerConfig:
    base = {
        "collector_endpoint": "",
        "project_name": "causal-oncall-test",
        "outcome_store_path": Path("/tmp/_unused_for_injected_store.jsonl"),
        "rolling_window_days": 30,
        "trend_buckets": 6,
    }
    base.update(overrides)
    return PhoenixTracerConfig(**base)


def test_dashboard_payload_from_real_tracer_with_no_outcomes_yields_zeros():
    """Cold start -- no eval rows yet -- renders without NaN gaps."""
    fake = FakePhoenixClient()
    tracer = PhoenixTracer(_cfg(trend_buckets=6), recorder=fake, outcome_store=fake)

    payload = dashboard_payload_from(tracer)

    assert payload.rolling_accuracy == 0.0
    assert payload.total_briefs == 0
    assert payload.confirmed_count == 0
    assert payload.trend_length == 6
    assert payload.starting_accuracy == 0.0
    # Empty buckets still render -- one zero per bucket so the chart line is flat.
    assert payload.trend == (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_dashboard_payload_from_real_tracer_reflects_recorded_outcomes():
    """Seeded outcomes flow through tracer -> AccuracyDashboardData -> payload."""
    fake = FakePhoenixClient()
    now = datetime(2026, 5, 20, 12, 0, tzinfo=UTC)
    for i in range(3):
        fake.seed_outcome(
            span_id=f"good-{i}",
            top_hypothesis_correct=True,
            recorded_at=now - timedelta(days=i),
        )
    fake.seed_outcome(
        span_id="bad-0",
        top_hypothesis_correct=False,
        recorded_at=now - timedelta(days=1),
    )
    tracer = PhoenixTracer(
        _cfg(trend_buckets=6),
        recorder=fake,
        outcome_store=fake,
        clock=lambda: now,
    )

    payload = dashboard_payload_from(tracer)

    assert payload.total_briefs == 4
    assert payload.confirmed_count == 3
    assert payload.rolling_accuracy == pytest.approx(3 / 4)
    assert payload.trend_length == 6
    # Most recent bucket is non-zero (data was seeded in the last few days).
    assert payload.trend[-1] > 0.0


# ---------------------------------------------------------------------------
# _from_accuracy() adapter -- starting_accuracy derivation
# ---------------------------------------------------------------------------


def test_from_accuracy_derives_starting_accuracy_from_first_trend_value():
    data = AccuracyDashboardData(
        rolling_accuracy=0.73,
        total_briefs=10,
        confirmed_count=7,
        trend=(0.41, 0.55, 0.73),
    )
    payload = _from_accuracy(data)
    assert payload.starting_accuracy == pytest.approx(0.41)
    assert payload.trend_length == 3
    assert payload.trend == (0.41, 0.55, 0.73)


def test_from_accuracy_starting_accuracy_is_zero_when_trend_is_empty():
    """Defensive: a zero-bucket tracer config produces an empty trend
    tuple; the payload must still render without an IndexError."""
    data = AccuracyDashboardData(
        rolling_accuracy=0.0,
        total_briefs=0,
        confirmed_count=0,
        trend=(),
    )
    payload = _from_accuracy(data)
    assert payload.starting_accuracy == 0.0
    assert payload.trend_length == 0
    assert payload.trend == ()


# ---------------------------------------------------------------------------
# render_dashboard_page() -- HTML serving
# ---------------------------------------------------------------------------


def test_render_dashboard_page_returns_self_improvement_html():
    """The page must announce itself with the locked title + accuracy label."""
    html = render_dashboard_page()
    assert "Causal On-Call: Self-Improvement" in html
    assert "top-hypothesis correct" in html
    # No external scripts -- works behind a corporate proxy that blocks CDNs.
    assert "<script src=" not in html


def test_render_dashboard_page_includes_sparkline_svg_and_auto_refresh():
    """Vanilla JS sparkline + 30s setInterval refresh -- no JS frameworks."""
    html = render_dashboard_page()
    # SVG sparkline element is present.
    assert "<svg" in html
    assert 'id="spark"' in html
    # Auto-refresh via setInterval at 30s.
    assert "setInterval" in html
    assert "30000" in html
    # Fetch the data endpoint -- bound to the same origin.
    assert "/dashboard/data" in html


def test_render_dashboard_page_supports_demo_query_param():
    """The page checks ``?demo=true`` and appends it to the data URL."""
    html = render_dashboard_page()
    assert "demo=true" in html
