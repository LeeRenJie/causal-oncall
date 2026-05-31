"""Hypothesis serialization — Brief domain objects to webhook JSON.

Hides: the per-hypothesis JSON shape the landing page consumes, including
the ``supporting_evidence`` rows the brief cards render, and the
"raw key as title" humanization that keeps a stray ``cve_exposure`` from
surfacing as a literal hypothesis title in the demo.

The public surface is two functions:

* ``serialize_ranked_hypotheses(brief)`` — list of hypothesis dicts the
  webhook + reject endpoints return verbatim.
* ``humanize_hypothesis_key(key)`` — turn a snake_case hypothesis key into
  a readable title (used only when a hypothesis title equals its raw key).
"""

from __future__ import annotations

from causal_oncall.domain.brief import Brief, Hypothesis
from causal_oncall.domain.evidence import Evidence

# Curated labels for the hypothesis keys the shipped specialists emit. A key
# missing here falls back to a generic title-cased rendering so the demo never
# shows a raw ``snake_case`` token as a hypothesis title.
_KEY_LABELS: dict[str, str] = {
    "db_pool_exhaustion": "Database connection pool exhaustion",
    "cve_exposure": "CVE exposure in a runtime dependency",
    "deploy_regression": "Deploy-induced regression",
    "topology_cascade": "Upstream dependency cascade",
    "anomaly_window": "Anomalous metric deviation in the incident window",
}


def humanize_hypothesis_key(key: str) -> str:
    """Render a snake_case hypothesis key as a readable title.

    Uses a curated label when one exists; otherwise title-cases the key with
    underscores turned into spaces. An empty key yields a stable placeholder
    rather than an empty string.
    """
    if not key:
        return "Unclassified hypothesis"
    curated = _KEY_LABELS.get(key)
    if curated is not None:
        return curated
    return key.replace("_", " ").strip().capitalize()


def _resolve_title(hypothesis: Hypothesis) -> str:
    """Return a human title, humanizing it when it equals the raw key.

    Specialists never set titles; the synthesizer normally does. When the
    LLM omits prose for a hypothesis the synthesizer falls back to the raw
    key as the title (e.g. ``cve_exposure``), which reads as broken in the
    demo. Detect that case and humanize it.
    """
    title = hypothesis.title
    if not title or title == hypothesis.key:
        return humanize_hypothesis_key(hypothesis.key)
    return title


def _serialize_evidence(evidence: Evidence) -> dict:
    """Serialize one Evidence into the row shape the brief cards render."""
    return {
        "specialist": evidence.specialist,
        "summary": evidence.summary,
        "confidence": evidence.confidence,
        "stance": evidence.stance,
    }


def _serialize_hypothesis(hypothesis: Hypothesis) -> dict:
    """Serialize one Hypothesis, including its supporting-evidence rows."""
    return {
        "rank": hypothesis.rank,
        "key": hypothesis.key,
        "title": _resolve_title(hypothesis),
        "score": hypothesis.score,
        "supporting_evidence": [_serialize_evidence(ev) for ev in hypothesis.supporting_evidence],
    }


def serialize_ranked_hypotheses(brief: Brief) -> list[dict]:
    """Serialize a brief's ranked hypotheses for the webhook JSON response.

    Each dict carries ``rank``, ``key``, ``title`` (humanized when it would
    otherwise be the raw key), ``score``, and a ``supporting_evidence`` array
    of ``{specialist, summary, confidence, stance}`` rows the landing page's
    brief cards consume to render evidence + the findings count.
    """
    return [_serialize_hypothesis(h) for h in brief.ranked_hypotheses]
