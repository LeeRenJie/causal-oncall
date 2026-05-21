"""Landing page + warmup + Grail-event viewer rendering (W4-S5).

This module is the testable counterpart to the FastAPI route handlers in
``app.py``. It hides:

- The on-disk static HTML page lookup for ``GET /`` (landing) and
  ``GET /grail-event/{problem_id}`` (a JSON-viewer page that lets the
  demo narration jump straight from a brief to "here is the CUSTOM_INFO
  event we wrote into Grail" without leaving the browser).
- The lightweight :class:`WarmupStatus` builder consumed by
  ``GET /warmup``. A pre-warm script (``scripts/prewarm.sh`` /
  ``scripts/prewarm.ps1``) pings this endpoint every 30 seconds for the
  five minutes preceding a demo take so the Cloud Run container is hot
  when the curl lands. No LLM/MCP calls, no Mongo round-trip — by
  design lightweight so the warmup itself never becomes the bottleneck.
- The static-HTML escape of ``problem_id`` on the Grail-event page so a
  pathological id (``"<script>"``) cannot escape the viewer chrome.

The FastAPI route handlers in ``app.py`` reduce to:

    * ``GET /``                       -> ``HTMLResponse(render_landing_page())``
    * ``GET /warmup``                 -> ``JSONResponse(build_warmup_status().to_dict())``
    * ``GET /grail-event/{problem_id}`` -> ``HTMLResponse(render_grail_event_page(problem_id))``

Why a separate module: the ``app.py`` route handlers are excluded from
the 100% coverage gate (framework wiring); the rendering + status logic
must stay covered, so it lives here. Mirrors the W3-S5 ``dashboard.py``
pattern.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

#: Path to the static HTML pages served at ``GET /`` and
#: ``GET /grail-event/{id}``. Co-located with this module so
#: ``setuptools`` ships them as package data (see ``pyproject.toml``
#: ``[tool.setuptools.package-data]``).
_STATIC_DIR = Path(__file__).parent / "static"
_LANDING_HTML = _STATIC_DIR / "landing.html"
_GRAIL_EVENT_HTML = _STATIC_DIR / "grail_event.html"

#: Process-start timestamp captured at module import. Used to derive
#: ``service_uptime_sec`` in :func:`build_warmup_status`. Captured once
#: rather than per-request so the value reflects "how long has this
#: Cloud Run instance been alive", not "how long since this handler
#: started". The pre-warm script reads the value to decide whether the
#: instance is fresh (a cold-start happened mid-warmup) or hot.
_PROCESS_START = time.monotonic()

#: Placeholder token replaced inside ``grail_event.html`` at render
#: time. Lives as a module constant so the template stays readable in
#: the static file and the replacement contract is testable.
_GRAIL_PROBLEM_ID_TOKEN = "__PROBLEM_ID__"


@dataclass(frozen=True, slots=True)
class WarmupStatus:
    """JSON-serializable view model for the ``GET /warmup`` endpoint.

    Attributes:
        warm: Always ``True`` while the process can handle a request.
            The pre-warm script treats any HTTP 200 with ``warm=true``
            as success.
        service_uptime_sec: Integer seconds since this process started.
            Strictly monotonic per instance — a value smaller than the
            previous poll means Cloud Run rotated the container.
        ts: ISO-8601 UTC timestamp at the moment the status was built.
            Lets the pre-warm script log clock-correlated checkpoints.
    """

    warm: bool
    service_uptime_sec: int
    ts: str

    def to_dict(self) -> dict:
        """JSON shape consumed by the pre-warm script + smoke tests."""
        return {
            "warm": self.warm,
            "service_uptime_sec": self.service_uptime_sec,
            "ts": self.ts,
        }


def build_warmup_status(now: datetime | None = None) -> WarmupStatus:
    """Return the current warmup status.

    Lightweight by contract — no LLM, no MCP, no Mongo. The pre-warm
    script hits this every 30 seconds for 5 minutes before recording;
    if it ever did real work the warmup itself would become the
    bottleneck and undermine its own purpose.

    The optional ``now`` parameter exists for deterministic testing;
    production callers leave it unset and get :func:`datetime.now`.
    """
    stamp = now if now is not None else datetime.now(UTC)
    uptime = int(time.monotonic() - _PROCESS_START)
    return WarmupStatus(
        warm=True,
        service_uptime_sec=uptime,
        ts=stamp.isoformat(),
    )


def render_landing_page() -> str:
    """Return the HTML body served at ``GET /``.

    Reads the co-located ``static/landing.html`` so the page is editable
    without touching Python. The file is excluded from the coverage
    gate -- it's data, not logic.
    """
    return _LANDING_HTML.read_text(encoding="utf-8")


def render_grail_event_page(problem_id: str) -> str:
    """Return the Grail CUSTOM_INFO event viewer for one problem id.

    Reads the co-located ``static/grail_event.html`` template and
    substitutes the problem id placeholder. The id is HTML-escaped so a
    pathological value (``"<script>alert(1)</script>"``) cannot escape
    the viewer chrome into a real script tag.
    """
    template = _GRAIL_EVENT_HTML.read_text(encoding="utf-8")
    safe_id = html.escape(problem_id, quote=True)
    return template.replace(_GRAIL_PROBLEM_ID_TOKEN, safe_id)
