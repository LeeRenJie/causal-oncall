"""Dashboard data + page rendering for the W3-S5 self-improvement wow moment.

Hides:
- The hand-crafted 30-day "demo mode" accuracy curve climbing 41% -> 73%
  that lets the 3-minute demo land cleanly without 6 months of real
  history (UNIQUE_IDEA wow factor #4).
- The on-disk static HTML page lookup (single ``dashboard.html`` next to
  this module under ``static/``).
- The shape conversion from :class:`AccuracyDashboardData` to the JSON
  body the page consumes via ``fetch('/dashboard/data')``.

The FastAPI route handlers in ``app.py`` reduce to:
    * ``GET /dashboard`` -> ``HTMLResponse(render_dashboard_page())``
    * ``GET /dashboard/data?demo=true`` -> ``JSONResponse(demo_dashboard_payload())``
    * ``GET /dashboard/data`` -> ``JSONResponse(dashboard_payload_from(tracer))``

Why a separate module: the route handlers in ``app.py`` are excluded
from the 100% coverage gate (framework wiring); the data/HTML rendering
must stay covered, so it lives here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from causal_oncall.phoenix_tracer import AccuracyDashboardData, PhoenixTracer

#: Path to the static HTML page served at ``GET /dashboard``. Co-located
#: with this module so ``setuptools`` ships it as package data (see
#: ``pyproject.toml`` ``[tool.setuptools.package-data]``).
_STATIC_DIR = Path(__file__).parent / "static"
_DASHBOARD_HTML = _STATIC_DIR / "dashboard.html"

#: Hand-crafted 30-day accuracy curve climbing 41% -> 73%. Powers the
#: ``?demo=true`` query param so the 3-minute demo lands the wow moment
#: even on a fresh database with no real 6-month history. Matches the
#: PLAN §5 beat: "Dashboard tab: rolling accuracy curve climbing 41% ->
#: 73% over 6 months". Length is exactly 30 entries -- one value per day.
_DEMO_TREND: tuple[float, ...] = (
    0.41,
    0.43,
    0.45,
    0.48,
    0.50,
    0.53,
    0.55,
    0.58,
    0.60,
    0.62,
    0.64,
    0.65,
    0.66,
    0.68,
    0.69,
    0.70,
    0.71,
    0.71,
    0.72,
    0.72,
    0.72,
    0.73,
    0.73,
    0.73,
    0.73,
    0.73,
    0.73,
    0.73,
    0.73,
    0.73,
)

#: Demo headline numbers -- match the wow-moment narration "147 briefs
#: over 30 days, 107 human-confirmed" (107/147 ~= 0.728 ~ 73%).
_DEMO_TOTAL_BRIEFS = 147
_DEMO_CONFIRMED_COUNT = 107


@dataclass(frozen=True, slots=True)
class DashboardPayload:
    """JSON-serializable view model for the dashboard page.

    Mirrors :class:`AccuracyDashboardData` 1:1 plus two derived fields
    (``starting_accuracy``, ``trend_length``) that the page renders in
    its caption without any client-side math.

    Attributes:
        rolling_accuracy: Latest rolling accuracy (0.0 -- 1.0). The big
            number top-center on the page.
        total_briefs: Briefs in the rolling window. Subtitle context.
        confirmed_count: Human-confirmed subset. Subtitle context.
        trend: One value per bucket/day, oldest first. Sparkline source.
        starting_accuracy: First value in ``trend`` -- powers the caption
            "up from X% in month 1". Zero when ``trend`` is empty.
        trend_length: ``len(trend)`` -- echoed so the page never
            recomputes it client-side.
    """

    rolling_accuracy: float
    total_briefs: int
    confirmed_count: int
    trend: tuple[float, ...]
    starting_accuracy: float
    trend_length: int

    def to_dict(self) -> dict:
        """JSON shape consumed by the page's ``fetch('/dashboard/data')`` call.

        Returns a list (not a tuple) for ``trend`` since ``json.dumps``
        already converts tuples to lists -- being explicit keeps the
        contract obvious to anyone reading the route handler.
        """
        return {
            "rolling_accuracy": self.rolling_accuracy,
            "total_briefs": self.total_briefs,
            "confirmed_count": self.confirmed_count,
            "trend": list(self.trend),
            "starting_accuracy": self.starting_accuracy,
            "trend_length": self.trend_length,
        }


def demo_dashboard_payload() -> DashboardPayload:
    """Return the hand-crafted 41% -> 73% curve for the live-demo wow path.

    Hit via ``GET /dashboard/data?demo=true``. The real data store has
    only fixtures -- not 6 months of operational history -- so demo
    mode lets the wow moment land cleanly during the 3-minute demo.
    """
    return DashboardPayload(
        rolling_accuracy=0.73,
        total_briefs=_DEMO_TOTAL_BRIEFS,
        confirmed_count=_DEMO_CONFIRMED_COUNT,
        trend=_DEMO_TREND,
        starting_accuracy=_DEMO_TREND[0],
        trend_length=len(_DEMO_TREND),
    )


def dashboard_payload_from(tracer: PhoenixTracer) -> DashboardPayload:
    """Build the payload from a live :class:`PhoenixTracer` snapshot.

    The tracer's :meth:`PhoenixTracer.accuracy_dashboard_data` already
    does the heavy lifting (rolling-window filter, bucketing); this
    function just adapts that data class to the JSON view model.
    """
    data = tracer.accuracy_dashboard_data()
    return _from_accuracy(data)


def _from_accuracy(data: AccuracyDashboardData) -> DashboardPayload:
    """Adapter: :class:`AccuracyDashboardData` -> :class:`DashboardPayload`.

    Derives ``starting_accuracy`` from the first trend value (0.0 when
    the trend is empty -- cold-start safe). ``trend_length`` is
    pre-computed so the page never has to count.
    """
    trend = tuple(data.trend)
    starting = trend[0] if trend else 0.0
    return DashboardPayload(
        rolling_accuracy=data.rolling_accuracy,
        total_briefs=data.total_briefs,
        confirmed_count=data.confirmed_count,
        trend=trend,
        starting_accuracy=starting,
        trend_length=len(trend),
    )


def render_dashboard_page() -> str:
    """Return the HTML body served at ``GET /dashboard``.

    Reads the co-located ``static/dashboard.html`` so the page is
    editable without touching Python. The file is excluded from the
    coverage gate -- it's data, not logic.
    """
    return _DASHBOARD_HTML.read_text(encoding="utf-8")
