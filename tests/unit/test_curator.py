"""TDD spec for Curator.

The curator is the slowest-moving piece of the learning loop and the
first to be cut if we slip. These tests pin the contract so that if
we *do* ship it, it behaves predictably.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from causal_oncall.curator import Curator, CuratorConfig
from causal_oncall.domain.incident_record import IncidentRecord
from tests.conftest import FakeMemoryStore, make_brief, make_signature


def _resolved(incident_id: str, root_cause: str) -> IncidentRecord:
    return IncidentRecord(
        incident_id=incident_id,
        signature=make_signature(fingerprint=f"fp-{incident_id}"),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key=root_cause,
        confirmed_fix="...",
    )


def test_curator_promotes_clusters_at_or_above_min_size(monkeypatch):
    memory = FakeMemoryStore()
    monkeypatch.setattr(
        memory,
        "list_resolved_since",
        lambda days: [
            _resolved("a", "db_pool_exhaustion"),
            _resolved("b", "db_pool_exhaustion"),
            _resolved("c", "db_pool_exhaustion"),
            _resolved("d", "noisy_neighbor"),  # singleton, below threshold
        ],
        raising=False,
    )
    promote = MagicMock()
    monkeypatch.setattr(memory, "promote_few_shot", promote, raising=False)

    curator = Curator(memory=memory, config=CuratorConfig(min_cluster_size=3))
    report = curator.run_weekly_batch()

    assert report.clusters_promoted == 1
    promote.assert_called_once()
    args, kwargs = promote.call_args
    # Whichever style of arg passing the impl uses, the promoted root
    # cause must be the recurring one, not the singleton.
    promoted_arg = (args + tuple(kwargs.values()))[0]
    assert "db_pool_exhaustion" in repr(promoted_arg)


def test_curator_report_names_the_top_recurring_pattern(monkeypatch):
    memory = FakeMemoryStore()
    monkeypatch.setattr(
        memory,
        "list_resolved_since",
        lambda days: [_resolved(f"x{i}", "db_pool_exhaustion") for i in range(5)],
        raising=False,
    )
    monkeypatch.setattr(memory, "promote_few_shot", lambda *a, **kw: None, raising=False)

    curator = Curator(memory=memory, config=CuratorConfig(min_cluster_size=3))
    report = curator.run_weekly_batch()
    assert "db_pool_exhaustion" in report.top_recurring_pattern


def test_curator_idempotent_on_already_promoted_clusters(monkeypatch):
    memory = FakeMemoryStore()
    monkeypatch.setattr(
        memory,
        "list_resolved_since",
        lambda days: [_resolved(f"x{i}", "db_pool_exhaustion") for i in range(5)],
        raising=False,
    )
    already_promoted = {"db_pool_exhaustion"}
    monkeypatch.setattr(
        memory,
        "already_promoted_keys",
        lambda: already_promoted,
        raising=False,
    )
    promote = MagicMock()
    monkeypatch.setattr(memory, "promote_few_shot", promote, raising=False)

    curator = Curator(memory=memory, config=CuratorConfig(min_cluster_size=3))
    report = curator.run_weekly_batch()
    assert report.clusters_promoted == 0
    promote.assert_not_called()


def test_curator_skips_records_with_no_confirmed_root_cause(monkeypatch):
    """Records lacking confirmed_root_cause_key are still-open; ignore them."""
    memory = FakeMemoryStore()
    open_record = IncidentRecord(
        incident_id="open-1",
        signature=make_signature(fingerprint="fp-open"),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        resolved_at=None,
        confirmed_root_cause_key=None,
    )
    monkeypatch.setattr(
        memory,
        "list_resolved_since",
        lambda days: [open_record],
        raising=False,
    )
    monkeypatch.setattr(memory, "promote_few_shot", lambda *a, **kw: None, raising=False)

    curator = Curator(memory=memory)
    report = curator.run_weekly_batch()
    assert report.clusters_promoted == 0
    assert "no recurring pattern" in report.top_recurring_pattern


def test_curator_handles_empty_window(monkeypatch):
    """Empty resolved-incident window produces a zero-cluster report."""
    memory = FakeMemoryStore()
    monkeypatch.setattr(memory, "list_resolved_since", lambda days: [], raising=False)
    monkeypatch.setattr(memory, "promote_few_shot", lambda *a, **kw: None, raising=False)

    curator = Curator(memory=memory)
    report = curator.run_weekly_batch()
    assert report.incidents_scanned == 0
    assert report.clusters_promoted == 0
