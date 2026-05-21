"""Integration tests for the W4-S5 landing page + warmup + Grail viewer.

Stands the FastAPI app up via ``TestClient`` in dev-mode wiring and
hits the four new routes. Asserts the HTTP contract end-to-end so the
``# pragma: no cover`` route handlers in ``app.py`` are exercised
through the real ASGI stack -- mirrors the W3-S5 dashboard pattern.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    """Boot the FastAPI app under dev-mode wiring with isolated tmp paths."""
    monkeypatch.setenv("CAUSAL_ONCALL_DEV_MODE", "1")
    monkeypatch.setenv("PHOENIX_OUTCOME_STORE_PATH", str(tmp_path / "phoenix_outcomes.jsonl"))
    monkeypatch.setenv("BRIEFS_OUTPUT_DIR", str(tmp_path / "briefs"))
    monkeypatch.delenv("PHOENIX_COLLECTOR_ENDPOINT", raising=False)

    import importlib

    import causal_oncall.app as app_module

    importlib.reload(app_module)
    with TestClient(app_module.app) as c:
        yield c


def test_landing_page_returns_html_with_hero_demo_buttons_and_sponsors(client: TestClient):
    """GET / returns the landing HTML with every demo-script-visible token."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # Hero
    assert "Causal On-Call" in body
    assert "at minute 15. At minute 1." in body
    # Three demo cards
    assert "Run cold investigation" in body
    assert "Run memory-hit (seen 14x before)" in body
    assert "Run with hypothesis rejection" in body
    # Sponsor footer pills
    assert "Dynatrace" in body
    assert "Google Cloud Agent Builder" in body
    assert "MongoDB Atlas" in body
    assert "Arize Phoenix" in body


def test_warmup_returns_lightweight_json_contract(client: TestClient):
    """GET /warmup returns ``warm=true`` + uptime + ts -- no LLM/MCP calls."""
    resp = client.get("/warmup")
    assert resp.status_code == 200
    assert "application/json" in resp.headers["content-type"]
    body = resp.json()
    assert body["warm"] is True
    assert isinstance(body["service_uptime_sec"], int)
    assert body["service_uptime_sec"] >= 0
    assert isinstance(body["ts"], str)
    assert "T" in body["ts"]  # ISO-8601


def test_grail_event_page_returns_html_with_problem_id_interpolated(client: TestClient):
    """GET /grail-event/{problem_id} returns the JSON viewer page."""
    resp = client.get("/grail-event/P-2026-05-17-001")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "P-2026-05-17-001" in body
    assert "CUSTOM_INFO event" in body
    assert "Grail event viewer" in body
    # The placeholder token must have been substituted.
    assert "__PROBLEM_ID__" not in body


def test_grail_event_page_escapes_pathological_problem_id(client: TestClient):
    """An HTML-special id is escaped in the rendered body, not raw.

    ``&`` and ``"`` are the two HTML-special chars Starlette will route
    through a path segment unscathed (angle brackets get URL-encoded
    out of the segment by the test client). Asserting both proves
    ``html.escape(..., quote=True)`` is the active code path.
    """
    resp = client.get('/grail-event/foo&bar"baz')
    assert resp.status_code == 200
    body = resp.text
    # Raw ``&bar"`` substring is gone -- replaced by escaped entities.
    assert 'foo&bar"baz' not in body
    assert "foo&amp;bar&quot;baz" in body


def test_dashboard_page_now_includes_sponsor_footer(client: TestClient):
    """W4-S5: the dashboard page gained a sponsor footer alongside the chart."""
    resp = client.get("/dashboard")
    assert resp.status_code == 200
    body = resp.text
    # The "Powered by" pre-text + at least three pill labels.
    assert "Powered by" in body
    assert "Dynatrace" in body
    assert "MongoDB Atlas" in body
    assert "Arize Phoenix" in body


def test_reject_endpoint_replans_brief_without_the_rejected_hypothesis(client: TestClient):
    """POST /webhook/dynatrace-problem/{id}/reject re-synthesises minus the key."""
    # Fire the cold-path webhook first so the orchestrator caches evidence.
    payload = {
        "problemId": "-9223372036854775807_v2",
        "title": "Response time degradation",
        "severityLevel": "PERFORMANCE",
        "startTime": "2026-05-17T09:30:00Z",
        "affectedEntities": [
            {"entityId": {"id": "SERVICE-ABC123", "type": "SERVICE"}, "type": "SERVICE"}
        ],
    }
    fire = client.post("/webhook/dynatrace-problem", json=payload)
    assert fire.status_code == 200
    first_brief = fire.json()
    assert first_brief["ranked_hypotheses"], "cold path must return at least one hypothesis"
    top_key = first_brief["ranked_hypotheses"][0]["key"]

    # Now reject the top hypothesis and check the replan strips it.
    reject = client.post(
        f"/webhook/dynatrace-problem/{payload['problemId']}/reject",
        params={"hypothesis_key": top_key},
    )
    assert reject.status_code == 200
    replanned = reject.json()
    assert replanned["rejected_hypothesis_key"] == top_key
    remaining_keys = [h["key"] for h in replanned["ranked_hypotheses"]]
    assert top_key not in remaining_keys


@pytest.fixture(autouse=True)
def _cleanup_env():
    """Belt-and-braces: clear our env overrides after the test."""
    yield
    for k in ("CAUSAL_ONCALL_DEV_MODE", "PHOENIX_OUTCOME_STORE_PATH", "BRIEFS_OUTPUT_DIR"):
        os.environ.pop(k, None)
