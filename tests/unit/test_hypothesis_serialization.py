"""Spec for hypothesis_serialization — webhook JSON shape for brief cards.

Covers the per-hypothesis serialization the landing page consumes: the
``supporting_evidence`` rows (BUG 1) and the raw-key humanization (BUG 2).
"""

from __future__ import annotations

from datetime import UTC, datetime

from causal_oncall.domain.brief import Brief, Hypothesis
from causal_oncall.domain.evidence import Evidence
from causal_oncall.hypothesis_serialization import (
    humanize_hypothesis_key,
    serialize_ranked_hypotheses,
)


def _evidence(specialist: str = "triage", hypothesis_key: str = "db_pool_exhaustion") -> Evidence:
    return Evidence(
        specialist=specialist,
        kind="metric_anomaly",
        summary=f"{specialist} found a deviation",
        stance="supports",
        hypothesis_key=hypothesis_key,
        confidence=0.82,
    )


def _hypothesis(
    *,
    key: str = "db_pool_exhaustion",
    title: str = "DB pool exhausted",
    rank: int = 1,
    score: float = 0.9,
    supporting: tuple[Evidence, ...] = (),
) -> Hypothesis:
    return Hypothesis(
        key=key,
        title=title,
        rank=rank,
        score=score,
        supporting_evidence=supporting,
        refuting_evidence=(),
        next_action="Do the thing.",
    )


def _brief(hypotheses: tuple[Hypothesis, ...]) -> Brief:
    return Brief(
        problem_id="P-1",
        generated_at=datetime(2026, 5, 17, 9, 31, tzinfo=UTC),
        ranked_hypotheses=hypotheses,
        top_recommendation="Top action",
    )


def test_serialize_includes_supporting_evidence_rows():
    ev1 = _evidence(specialist="triage")
    ev2 = _evidence(specialist="deploy_correlation")
    brief = _brief((_hypothesis(supporting=(ev1, ev2)),))

    rows = serialize_ranked_hypotheses(brief)
    assert len(rows) == 1
    evidence = rows[0]["supporting_evidence"]
    assert len(evidence) == 2
    assert evidence[0] == {
        "specialist": "triage",
        "summary": "triage found a deviation",
        "confidence": 0.82,
        "stance": "supports",
    }
    assert evidence[1]["specialist"] == "deploy_correlation"


def test_serialize_preserves_core_fields():
    brief = _brief(
        (_hypothesis(key="db_pool_exhaustion", title="DB pool exhausted", rank=2, score=0.7),)
    )
    row = serialize_ranked_hypotheses(brief)[0]
    assert row["rank"] == 2
    assert row["key"] == "db_pool_exhaustion"
    assert row["title"] == "DB pool exhausted"
    assert row["score"] == 0.7


def test_serialize_empty_supporting_evidence_is_empty_list():
    brief = _brief((_hypothesis(supporting=()),))
    assert serialize_ranked_hypotheses(brief)[0]["supporting_evidence"] == []


def test_serialize_empty_brief_yields_empty_list():
    brief = _brief(())
    assert serialize_ranked_hypotheses(brief) == []


def test_title_equal_to_key_is_humanized_via_curated_label():
    brief = _brief((_hypothesis(key="cve_exposure", title="cve_exposure"),))
    assert serialize_ranked_hypotheses(brief)[0]["title"] == "CVE exposure in a runtime dependency"


def test_empty_title_falls_back_to_humanized_key():
    brief = _brief((_hypothesis(key="db_pool_exhaustion", title=""),))
    assert serialize_ranked_hypotheses(brief)[0]["title"] == "Database connection pool exhaustion"


def test_humanize_known_key_uses_curated_label():
    assert humanize_hypothesis_key("deploy_regression") == "Deploy-induced regression"


def test_humanize_unknown_key_title_cases():
    assert humanize_hypothesis_key("some_other_thing") == "Some other thing"


def test_humanize_empty_key_returns_placeholder():
    assert humanize_hypothesis_key("") == "Unclassified hypothesis"
