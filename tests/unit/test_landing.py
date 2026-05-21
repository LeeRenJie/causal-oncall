"""Unit tests for ``causal_oncall.landing`` (W4-S5).

The route handlers in ``app.py`` are coverage-excluded glue; the
behaviour lives here in narrow helpers. Tests cover:

* ``render_landing_page`` returns the static HTML body with the hero
  copy, the three demo-button labels, and the sponsor footer markup.
* ``render_grail_event_page`` substitutes the problem id placeholder
  *and* HTML-escapes pathological ids so the viewer chrome cannot be
  XSS'd.
* ``build_warmup_status`` returns a deterministic, lightweight status
  object (``warm=True``, monotonic-non-negative uptime, ISO timestamp).
* ``WarmupStatus.to_dict`` is the contract consumed by the pre-warm
  script + the smoke tests; the keys + types are pinned here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from causal_oncall.landing import (
    WarmupStatus,
    build_warmup_status,
    render_grail_event_page,
    render_landing_page,
)


def test_render_landing_page_contains_hero_demo_buttons_and_sponsor_footer():
    """Landing HTML carries every demo-script-visible token.

    Asserting these strings means the strategist's smoke check
    ("paste GET / and confirm H1 + 3 button labels appear in the
    HTML") never silently regresses if someone re-skins the page.
    """
    body = render_landing_page()
    # Hero copy — H1 phrasing + brand title
    assert "Causal On-Call" in body  # appears in the <title> tag
    assert "ADK multi-agent SRE assistant" in body
    assert "at minute 15, at minute 1." in body
    # Three demo card labels
    assert "Run cold investigation" in body
    assert "Run memory-hit (seen 14x before)" in body
    assert "Run with hypothesis rejection" in body
    # Sponsor footer pills (CSS only -- text + class names)
    assert "Dynatrace" in body
    assert "Google Cloud Agent Builder" in body
    assert "MongoDB Atlas" in body
    assert "Arize Phoenix" in body
    assert "Gemini 3" in body
    # SSE wiring is inline (no separate JS file).
    assert "EventSource" in body
    assert "renderBriefCards" in body
    # Impeccable design hard floors: OKLCH-only palette, motion.dev loaded,
    # no `#000` / `#fff` leaked, no gradient text, no glassmorphism, no em
    # dashes in user-facing copy.
    assert "oklch(" in body
    assert "motion@latest/+esm" in body
    assert "#000" not in body
    assert "#fff" not in body
    assert "background-clip: text" not in body  # gradient text ban
    assert "backdrop-filter" not in body  # glassmorphism ban
    # The em dash (—) must not appear in user-facing copy. The CSS does
    # use a unicode minus glyph for the accordion collapse marker; that's
    # the only legitimate non-ASCII punctuation. We allow it explicitly.
    assert "—" not in body  # em dash


def test_render_grail_event_page_substitutes_problem_id():
    """The viewer chrome interpolates the problem id at three callsites."""
    html_body = render_grail_event_page("P-2026-05-17-001")
    assert "P-2026-05-17-001" in html_body
    # The placeholder token must be gone after rendering.
    assert "__PROBLEM_ID__" not in html_body


def test_render_grail_event_page_escapes_html_in_problem_id():
    """An XSS-attempting id is HTML-escaped, not rendered as a script tag.

    The route handler accepts ``{problem_id}`` from the URL path; we
    must never trust that string verbatim in the rendered HTML.
    """
    html_body = render_grail_event_page("<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html_body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_body


def test_build_warmup_status_returns_warm_with_monotonic_uptime_and_iso_ts():
    """Default call -- warm=True, uptime>=0 int, ts is ISO-8601 UTC."""
    status = build_warmup_status()
    assert isinstance(status, WarmupStatus)
    assert status.warm is True
    assert isinstance(status.service_uptime_sec, int)
    assert status.service_uptime_sec >= 0
    # ISO-8601 with timezone -- fromisoformat tolerates the Z-less form.
    parsed = datetime.fromisoformat(status.ts)
    assert parsed.tzinfo is not None


def test_build_warmup_status_accepts_injected_now_for_determinism():
    """The optional ``now`` param lets tests pin the timestamp."""
    fixed = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
    status = build_warmup_status(now=fixed)
    assert status.ts == "2026-05-21T12:00:00+00:00"
    assert status.warm is True


def test_warmup_status_to_dict_pins_the_json_contract():
    """``to_dict`` returns exactly the three keys the script + tests read."""
    status = WarmupStatus(warm=True, service_uptime_sec=42, ts="2026-05-21T12:00:00+00:00")
    payload = status.to_dict()
    assert payload == {
        "warm": True,
        "service_uptime_sec": 42,
        "ts": "2026-05-21T12:00:00+00:00",
    }
