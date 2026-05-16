"""TDD spec for IncidentRecord + Match.

These are pure data types; the tests pin the structural contract the
memory store and curator depend on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from causal_oncall.domain.incident_record import IncidentRecord, Match
from tests.conftest import make_brief, make_signature


def test_record_defaults_to_unresolved():
    sig = make_signature()
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=sig,
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, 9, 30, tzinfo=UTC),
    )
    assert rec.resolved_at is None
    assert rec.confirmed_root_cause_key is None
    assert rec.confirmed_fix == ""
    assert rec.embedding == ()


def test_record_with_resolution_is_match_eligible():
    """Memory match() must never return unresolved rows; resolved ones may."""
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=make_signature(),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, 9, 30, tzinfo=UTC),
        resolved_at=datetime(2026, 5, 17, 10, 0, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased HikariCP pool size from 10 to 50.",
    )
    assert rec.confirmed_root_cause_key == "db_pool_exhaustion"
    assert rec.resolved_at is not None


def test_match_carries_similarity_and_prior_occurrence_count():
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=make_signature(),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, 9, 30, tzinfo=UTC),
        resolved_at=datetime(2026, 5, 17, 10, 0, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="...",
    )
    m = Match(record=rec, similarity=0.91, prior_occurrences=14)
    assert 0.0 <= m.similarity <= 1.0
    assert m.prior_occurrences == 14
