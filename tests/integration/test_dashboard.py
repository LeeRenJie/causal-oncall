"""Integration test for the W3-S5 self-improvement dashboard routes.

Stands the FastAPI app up via ``TestClient`` in dev-mode wiring, then
hits ``GET /dashboard`` and ``GET /dashboard/data`` (with and without
``?demo=true``). Asserts the HTTP contract end-to-end so the
``# pragma: no cover`` route handlers in ``app.py`` are exercised
through the real ASGI stack.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    """Boot the FastAPI app under dev-mode wiring with a temp outcome store."""
    monkeypatch.setenv("CAUSAL_ONCALL_DEV_MODE", "1")
    # Isolate the JSONL outcome store so this test never touches a shared
    # ./out/ directory across CI runs.
    monkeypatch.setenv("PHOENIX_OUTCOME_STORE_PATH", str(tmp_path / "phoenix_outcomes.jsonl"))
    monkeypatch.setenv("BRIEFS_OUTPUT_DIR", str(tmp_path / "briefs"))
    # Force any leftover OTLP env from a developer's shell off so we use
    # the stdout fallback recorder and never try to reach a live collector.
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)

    # Re-import the app so module-level state picks up the env override.
    import importlib

    import causal_oncall.app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        yield c


def test_dashboard_page_returns_html_with_the_wow_moment_chrome(client: TestClient):
    """GET /dashboard returns the self-improvement dashboard HTML page."""
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "Causal On-Call: Self-Improvement" in body
    assert "top-hypothesis correct" in body
    # SVG sparkline + auto-refresh wiring is present.
    assert "<svg" in body
    assert "setInterval" in body


def test_dashboard_data_demo_returns_the_canned_41_to_73_curve(client: TestClient):
    """?demo=true returns the hand-crafted 30-day climb (wow moment #4)."""
    resp = client.get("/dashboard/data", params={"demo": "true"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rolling_accuracy"] == pytest.approx(0.73)
    assert body["starting_accuracy"] == pytest.approx(0.41)
    assert body["total_briefs"] == 147
    assert body["confirmed_count"] == 107
    assert body["trend_length"] == 30
    assert len(body["trend"]) == 30
    assert body["trend"][0] == pytest.approx(0.41)
    assert body["trend"][-1] == pytest.approx(0.73)


def test_dashboard_data_without_demo_reads_from_real_tracer(client: TestClient):
    """No ``demo`` param -> reads from the live PhoenixTracer (empty cold start)."""
    resp = client.get("/dashboard/data")
    assert resp.status_code == 200
    body = resp.json()
    # Cold start: no recorded outcomes -> zero accuracy + zeroed trend.
    assert body["rolling_accuracy"] == 0.0
    assert body["total_briefs"] == 0
    assert body["confirmed_count"] == 0
    assert body["trend_length"] == 6  # default trend_buckets from config
    assert all(v == 0.0 for v in body["trend"])


@pytest.fixture(autouse=True)
def _cleanup_env():
    """Belt-and-braces: clear our env overrides after the test so the
    suite's next module sees a clean environment."""
    yield
    for k in ("CAUSAL_ONCALL_DEV_MODE", "PHOENIX_OUTCOME_STORE_PATH", "BRIEFS_OUTPUT_DIR"):
        os.environ.pop(k, None)
