"""TDD spec for MemoryStore.

Mongo is faked via mongomock so the test never opens a network socket.
The embedding model is faked through a deterministic stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from causal_oncall.domain.incident_record import IncidentRecord
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.memory_store import MemoryStore, MemoryStoreConfig
from tests.conftest import make_brief, make_signature


def _cfg(**overrides) -> MemoryStoreConfig:
    base = dict(
        mongodb_uri="mongodb://localhost:27017",
        database="test_db",
        collection="incidents",
        vector_index_name="incident_signature_vector",
        embedding_model_id="fake-embed",
        embedding_dimensions=4,
        match_threshold=0.85,
    )
    base.update(overrides)
    return MemoryStoreConfig(**base)


def _install_fakes(store: MemoryStore, monkeypatch, *, records: list[dict[str, Any]]):
    """Patch the store's Mongo + embedder seams to in-process fakes."""
    import mongomock

    client = mongomock.MongoClient()
    coll = client["test_db"]["incidents"]
    for r in records:
        coll.insert_one(dict(r))

    monkeypatch.setattr(store, "_collection", coll, raising=False)
    monkeypatch.setattr(store, "_embed", lambda text: (1.0, 0.0, 0.0, 0.0), raising=False)
    return coll


def test_match_returns_none_below_threshold(monkeypatch):
    store = MemoryStore(_cfg(match_threshold=0.99))
    _install_fakes(
        store,
        monkeypatch,
        records=[
            {
                "fingerprint": "fp-old",
                "embedding": [0.0, 1.0, 0.0, 0.0],  # orthogonal -> low cosine
                "confirmed_root_cause_key": "db_pool_exhaustion",
                "confirmed_fix": "...",
                "incident_id": "old-1",
            }
        ],
    )
    assert store.match(make_signature(fingerprint="fp-new")) is None


def test_match_returns_record_above_threshold(monkeypatch):
    store = MemoryStore(_cfg(match_threshold=0.5))
    _install_fakes(
        store,
        monkeypatch,
        records=[
            {
                "fingerprint": "fp-old",
                "embedding": [1.0, 0.0, 0.0, 0.0],  # identical -> cosine=1
                "confirmed_root_cause_key": "db_pool_exhaustion",
                "confirmed_fix": "Increased pool size",
                "incident_id": "old-1",
                "opened_at": datetime(2026, 4, 1, tzinfo=UTC),
                "resolved_at": datetime(2026, 4, 1, 1, tzinfo=UTC),
            }
        ],
    )
    m = store.match(make_signature(fingerprint="fp-new"))
    assert m is not None
    assert m.similarity >= 0.99
    assert m.record.confirmed_root_cause_key == "db_pool_exhaustion"


def test_match_never_returns_an_unresolved_record(monkeypatch):
    store = MemoryStore(_cfg(match_threshold=0.5))
    _install_fakes(
        store,
        monkeypatch,
        records=[
            {
                "fingerprint": "fp-old",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "confirmed_root_cause_key": None,
                "confirmed_fix": "",
                "incident_id": "open-1",
            }
        ],
    )
    assert store.match(make_signature(fingerprint="fp-new")) is None


def test_match_populates_prior_occurrences_for_same_root_cause(monkeypatch):
    store = MemoryStore(_cfg(match_threshold=0.5))
    _install_fakes(
        store,
        monkeypatch,
        records=[
            {
                "fingerprint": f"fp-{i}",
                "embedding": [1.0, 0.0, 0.0, 0.0],
                "confirmed_root_cause_key": "db_pool_exhaustion",
                "confirmed_fix": "...",
                "incident_id": f"old-{i}",
            }
            for i in range(3)
        ],
    )
    m = store.match(make_signature(fingerprint="fp-new"))
    assert m is not None
    assert m.prior_occurrences == 3


def test_record_dedup_is_keyed_on_fingerprint(monkeypatch):
    store = MemoryStore(_cfg())
    coll = _install_fakes(store, monkeypatch, records=[])

    sig = make_signature(fingerprint="fp-X")
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=sig,
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    store.record(rec)
    store.record(rec)
    assert coll.count_documents({}) == 1


def test_update_resolution_attaches_human_verdict(monkeypatch):
    store = MemoryStore(_cfg())
    coll = _install_fakes(
        store,
        monkeypatch,
        records=[{"incident_id": "inc-1", "fingerprint": "fp"}],
    )
    store.update_resolution(
        "inc-1",
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased pool size",
    )
    doc = coll.find_one({"incident_id": "inc-1"})
    assert doc is not None
    assert doc["confirmed_root_cause_key"] == "db_pool_exhaustion"
    assert doc["confirmed_fix"] == "Increased pool size"
    assert doc.get("resolved_at") is not None
