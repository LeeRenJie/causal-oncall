"""TDD spec for MemoryStore.

Mongo is faked via ``FakeMongoCollection`` (tests/fakes/mongo.py), which
emulates Atlas's ``$vectorSearch`` aggregation shape end-to-end so the
production pipeline gets exercised at the unit layer without a network
hop. The embedder is faked through a deterministic ``FakeEmbedder``
(tests/fakes/vertex_embedder.py). Real Vertex AI + real Atlas are
covered by ``tests/contract/test_memory_store_contract.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from causal_oncall.domain.exceptions import MemoryStoreUnavailable
from causal_oncall.domain.incident_record import IncidentRecord
from causal_oncall.memory_store import MemoryStore, MemoryStoreConfig, _cosine, _hash_text
from tests.conftest import make_brief, make_memory_store_config, make_signature
from tests.fakes import FakeEmbedder, FakeMongoCollection

# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


def _store(
    *,
    embedder: FakeEmbedder | None = None,
    collection: FakeMongoCollection | None = None,
    match_threshold: float = 0.85,
    dim: int = 8,
) -> tuple[MemoryStore, FakeMongoCollection, FakeEmbedder]:
    coll = collection if collection is not None else FakeMongoCollection()
    emb = embedder if embedder is not None else FakeEmbedder(dim=dim)
    store = MemoryStore(
        make_memory_store_config(match_threshold=match_threshold, dim=dim),
        embedder=emb,
        collection=coll,
    )
    return store, coll, emb


def _seed_doc(
    *,
    embedding: list[float],
    confirmed_root_cause_key: str | None = "db_pool_exhaustion",
    confirmed_fix: str = "Increased HikariCP pool size",
    incident_id: str = "seed-1",
    signature_hash: str = "fp-seed",
    brief_hash: str = "bh-seed",
    title: str = "Response time degradation on payment-service",
    opened_at: datetime | None = None,
) -> dict:
    return {
        "incident_id": incident_id,
        "problem_signature_hash": signature_hash,
        "brief_hash": brief_hash,
        "embedding": embedding,
        "confirmed_root_cause_key": confirmed_root_cause_key,
        "confirmed_fix": confirmed_fix,
        "opened_at": opened_at or datetime(2026, 4, 1, tzinfo=UTC),
        "resolved_at": datetime(2026, 4, 1, 1, tzinfo=UTC),
        "signature": {
            "problem_id": "PROBLEM-OLD-1",
            "title": title,
            "severity": "PERFORMANCE",
            "affected_entity_ids": ["SERVICE-OLD"],
            "affected_entity_types": ["SERVICE"],
            "opened_at": opened_at or datetime(2026, 4, 1, tzinfo=UTC),
            "fingerprint": signature_hash,
        },
        "brief_markdown": "# Prior brief\n",
    }


# ---------------------------------------------------------------------- #
# match()
# ---------------------------------------------------------------------- #


def test_match_returns_none_when_corpus_is_empty():
    store, _, _ = _store(match_threshold=0.5)
    assert store.match(make_signature(fingerprint="fp-new")) is None


def test_match_returns_none_below_threshold():
    store, coll, emb = _store(match_threshold=0.99)
    # Seed an orthogonal vector that will score ~0 under cosine — below
    # any threshold > 0.
    query_vec = list(emb("severity=PERFORMANCE; title=anything; entity_types=SERVICE"))
    orthogonal = [-x for x in query_vec]  # cosine = -1, below any reasonable threshold
    coll.insert_one(_seed_doc(embedding=orthogonal))
    assert store.match(make_signature(fingerprint="fp-new")) is None


def test_match_returns_record_above_threshold():
    store, coll, emb = _store(match_threshold=0.5)
    # Seed with the *same* vector the query will produce so cosine = 1.
    query_vec = list(emb(make_signature().to_embedding_text()))
    # The embedder call inside _store made the embedder cache that text;
    # reset call log so we can assert on the production-path call.
    emb.calls.clear()

    coll.insert_one(_seed_doc(embedding=query_vec))
    m = store.match(make_signature())
    assert m is not None
    assert m.similarity == pytest.approx(1.0, rel=1e-6)
    assert m.record.confirmed_root_cause_key == "db_pool_exhaustion"
    # Match path embeds exactly once per call.
    assert len(emb.calls) == 1


def test_match_never_returns_an_unresolved_record():
    """Open incidents (no confirmed root cause yet) must not surface as matches."""
    store, coll, emb = _store(match_threshold=0.5)
    query_vec = list(emb(make_signature().to_embedding_text()))
    emb.calls.clear()
    coll.insert_one(_seed_doc(embedding=query_vec, confirmed_root_cause_key=None))
    assert store.match(make_signature()) is None


def test_match_populates_prior_occurrences_for_same_root_cause():
    store, coll, emb = _store(match_threshold=0.5)
    query_vec = list(emb(make_signature().to_embedding_text()))
    emb.calls.clear()
    for i in range(3):
        coll.insert_one(
            _seed_doc(
                embedding=query_vec,
                incident_id=f"seed-{i}",
                signature_hash=f"fp-seed-{i}",
                brief_hash=f"bh-seed-{i}",
            )
        )
    m = store.match(make_signature())
    assert m is not None
    assert m.prior_occurrences == 3


def test_match_picks_highest_score_when_multiple_above_threshold():
    """Two seeded records, both above threshold; the higher-score one wins."""
    store, coll, emb = _store(match_threshold=0.5)
    query_vec = list(emb(make_signature().to_embedding_text()))
    emb.calls.clear()

    # Exact match: cosine = 1.0
    coll.insert_one(_seed_doc(embedding=query_vec, incident_id="winner"))
    # Slightly perturbed (still well above 0.5): scale by 0.7 then add noise on first dim
    perturbed = [(x * 0.7) for x in query_vec]
    perturbed[0] += 0.1
    coll.insert_one(_seed_doc(embedding=perturbed, incident_id="runner-up"))

    m = store.match(make_signature())
    assert m is not None
    assert m.record.incident_id == "winner"


def test_match_threshold_override_takes_precedence_over_config_default():
    store, coll, emb = _store(match_threshold=0.99)
    query_vec = list(emb(make_signature().to_embedding_text()))
    emb.calls.clear()
    # Construct a vector with cosine ~0 against query_vec by flipping
    # half the components. Cosine cleanly fails 0.99 and passes any
    # threshold ≤ 0 — which is what the override path exists for.
    flipped = [-x if i < len(query_vec) // 2 else x for i, x in enumerate(query_vec)]
    coll.insert_one(_seed_doc(embedding=flipped))
    assert store.match(make_signature()) is None  # default config threshold = 0.99
    assert store.match(make_signature(), threshold=-1.0) is not None


def test_match_emits_pipeline_with_correct_index_path_and_filter():
    """Pin the wire-shape contract: the production pipeline must request the
    locked vector index, point at the ``embedding`` field, and filter out
    unresolved rows."""
    store, coll, emb = _store(match_threshold=0.5)
    coll.insert_one(_seed_doc(embedding=list(emb(make_signature().to_embedding_text()))))
    store.match(make_signature())
    pipeline = coll.last_aggregate_pipeline
    assert pipeline is not None
    assert pipeline[0]["$vectorSearch"]["index"] == "incident_vec_idx"
    assert pipeline[0]["$vectorSearch"]["path"] == "embedding"
    assert pipeline[0]["$vectorSearch"]["limit"] == 10
    assert pipeline[0]["$vectorSearch"]["numCandidates"] == 100
    assert pipeline[0]["$vectorSearch"]["filter"] == {
        "confirmed_root_cause_key": {"$exists": True, "$ne": None}
    }
    assert "$addFields" in pipeline[1]


def test_match_translates_atlas_exception_to_memory_store_unavailable():
    """When ``aggregate`` blows up (network, missing index, etc.) the caller
    sees a domain exception, never the raw pymongo error."""

    class _BoomCollection:
        def aggregate(self, _pipeline):
            raise RuntimeError("Atlas: $vectorSearch index missing")

    store, _, _ = _store(collection=_BoomCollection())  # type: ignore[arg-type]
    with pytest.raises(MemoryStoreUnavailable, match="vectorSearch"):
        store.match(make_signature())


# ---------------------------------------------------------------------- #
# record()
# ---------------------------------------------------------------------- #


def test_record_inserts_when_signature_and_brief_are_both_new():
    store, coll, _ = _store()
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=make_signature(fingerprint="fp-A"),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    store.record(rec)
    assert coll.count_documents({}) == 1
    stored = coll.find_one({})
    assert stored is not None
    assert stored["problem_signature_hash"] == "fp-A"
    assert stored["brief_hash"] == _hash_text(rec.brief.to_markdown())
    assert "created_at" in stored
    assert "updated_at" in stored


def test_record_dedup_is_keyed_on_signature_hash_and_brief_hash():
    """Same signature + same rendered brief => update in place, not insert."""
    store, coll, _ = _store()
    rec = IncidentRecord(
        incident_id="inc-1",
        signature=make_signature(fingerprint="fp-X"),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    store.record(rec)
    store.record(rec)
    assert coll.count_documents({}) == 1


def test_record_inserts_new_row_when_brief_changes_for_same_signature():
    """Same signature + different brief (e.g. specialists found more evidence)
    => preserve history with a fresh row."""
    store, coll, _ = _store()
    base = make_signature(fingerprint="fp-Y")
    rec1 = IncidentRecord(
        incident_id="inc-1",
        signature=base,
        brief=make_brief(problem_id="P1"),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    rec2 = IncidentRecord(
        incident_id="inc-2",
        signature=base,
        brief=make_brief(problem_id="P2"),  # different markdown -> different brief_hash
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    store.record(rec1)
    store.record(rec2)
    assert coll.count_documents({}) == 2


def test_record_persists_embedding_when_record_already_carries_one():
    """If the IncidentRecord pre-supplies an embedding, we don't re-embed."""
    store, coll, emb = _store()
    emb.calls.clear()
    rec = IncidentRecord(
        incident_id="inc-2",
        signature=make_signature(fingerprint="fp-Y"),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
        embedding=(0.5, 0.5, 0.5, 0.5),
    )
    store.record(rec)
    doc = coll.find_one({"incident_id": "inc-2"})
    assert doc is not None
    assert doc["embedding"] == [0.5, 0.5, 0.5, 0.5]
    assert emb.calls == []  # embedder must NOT be invoked when one is supplied


def test_record_generates_embedding_when_record_has_no_embedding():
    store, coll, emb = _store()
    emb.calls.clear()
    rec = IncidentRecord(
        incident_id="inc-3",
        signature=make_signature(fingerprint="fp-Z"),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
    )
    store.record(rec)
    doc = coll.find_one({"incident_id": "inc-3"})
    assert doc is not None
    assert len(doc["embedding"]) == 8  # fake embedder dim
    assert emb.calls == [rec.signature.to_embedding_text()]


def test_record_persists_all_signature_fields_for_later_rehydration():
    store, coll, _ = _store()
    sig = make_signature(
        problem_id="P-ABC",
        title="Latency anomaly",
        severity="PERFORMANCE",
        entity_ids=("SERVICE-A", "SERVICE-B"),
        entity_types=("SERVICE",),
        fingerprint="fp-rich",
    )
    rec = IncidentRecord(
        incident_id="inc-rich",
        signature=sig,
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased pool size",
    )
    store.record(rec)
    doc = coll.find_one({"incident_id": "inc-rich"})
    assert doc is not None
    assert doc["signature"]["problem_id"] == "P-ABC"
    assert doc["signature"]["affected_entity_ids"] == ["SERVICE-A", "SERVICE-B"]
    assert doc["confirmed_root_cause_key"] == "db_pool_exhaustion"
    assert doc["confirmed_fix"] == "Increased pool size"


def test_record_translates_atlas_exception_to_memory_store_unavailable():
    class _BoomCollection:
        def update_one(self, *_args, **_kwargs):
            raise RuntimeError("Atlas: write blocked by IP allowlist")

    store, _, _ = _store(collection=_BoomCollection())  # type: ignore[arg-type]
    rec = IncidentRecord(
        incident_id="inc-err",
        signature=make_signature(fingerprint="fp-err"),
        brief=make_brief(),
        opened_at=datetime(2026, 5, 17, tzinfo=UTC),
        embedding=(0.1,) * 8,
    )
    with pytest.raises(MemoryStoreUnavailable, match="upsert"):
        store.record(rec)


# ---------------------------------------------------------------------- #
# update_resolution()
# ---------------------------------------------------------------------- #


def test_update_resolution_attaches_human_verdict():
    store, coll, _ = _store()
    coll.insert_one(
        {
            "incident_id": "inc-1",
            "problem_signature_hash": "fp",
            "brief_hash": "bh",
        }
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
    assert doc["resolved_at"] is not None
    assert doc["updated_at"] is not None


def test_update_resolution_translates_atlas_exception_to_memory_store_unavailable():
    class _BoomCollection:
        def update_one(self, *_args, **_kwargs):
            raise RuntimeError("Atlas: connection reset by peer")

    store, _, _ = _store(collection=_BoomCollection())  # type: ignore[arg-type]
    with pytest.raises(MemoryStoreUnavailable, match="update"):
        store.update_resolution(
            "inc-x",
            confirmed_root_cause_key="db_pool_exhaustion",
            confirmed_fix="...",
        )


# ---------------------------------------------------------------------- #
# Module-private helpers
# ---------------------------------------------------------------------- #


def test_cosine_returns_zero_on_zero_norm_vector():
    """A zero vector against anything is degenerate; we surface 0.0 rather than nan."""
    assert _cosine([0.0, 0.0, 0.0], [1.0, 2.0, 3.0]) == 0.0
    assert _cosine([1.0, 2.0, 3.0], [0.0, 0.0, 0.0]) == 0.0


def test_cosine_returns_one_on_identical_vector():
    assert _cosine([1.0, 0.0, 0.0], [1.0, 0.0, 0.0]) == pytest.approx(1.0)


def test_hash_text_is_deterministic_and_short():
    a = _hash_text("hello")
    b = _hash_text("hello")
    c = _hash_text("world")
    assert a == b
    assert a != c
    assert len(a) == 16


# ---------------------------------------------------------------------- #
# Config + construction
# ---------------------------------------------------------------------- #


def test_memory_store_config_defaults_to_zero_eight_five_threshold():
    cfg = MemoryStoreConfig(
        mongodb_uri="mongodb://x",
        database="db",
        collection="incidents",
        vector_index_name="incident_vec_idx",
        embedding_model_id="text-embedding-005",
        embedding_dimensions=768,
    )
    assert cfg.match_threshold == 0.85
    assert cfg.vector_search_num_candidates == 100


def test_memory_store_uses_default_embedder_attribute_when_none_passed():
    """Verifies the constructor defaults to the lazy Vertex AI seam."""
    cfg = MemoryStoreConfig(
        mongodb_uri="mongodb://x",
        database="db",
        collection="incidents",
        vector_index_name="incident_vec_idx",
        embedding_model_id="text-embedding-005",
        embedding_dimensions=768,
    )
    store = MemoryStore(cfg)
    # _embed bound method points at the lazy default; don't invoke it
    # (would require Vertex AI creds), just assert identity.
    assert store._embed.__func__ is MemoryStore._default_embed  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- #
# W3-S3: list_resolved_since + list_active_few_shot_keys
# ---------------------------------------------------------------------- #


def test_list_resolved_since_returns_only_resolved_records_in_window():
    """W3-S3: Curator-facing batch read."""
    store, coll, _ = _store()
    base = datetime(2026, 5, 1, tzinfo=UTC)
    # Two resolved inside the window, sorted out-of-order to verify the sort.
    coll.insert_one(
        _seed_doc(embedding=[0.1] * 8, incident_id="late", opened_at=base)
        | {"resolved_at": datetime(2026, 5, 5, tzinfo=UTC)}
    )
    coll.insert_one(
        _seed_doc(embedding=[0.1] * 8, incident_id="early", opened_at=base)
        | {"resolved_at": datetime(2026, 5, 2, tzinfo=UTC)}
    )
    # One open (must be filtered).
    open_doc = _seed_doc(embedding=[0.1] * 8, incident_id="open")
    open_doc["confirmed_root_cause_key"] = None
    open_doc["resolved_at"] = datetime(2026, 5, 3, tzinfo=UTC)
    coll.insert_one(open_doc)
    # One outside the window (resolved before ``since``).
    coll.insert_one(
        _seed_doc(embedding=[0.1] * 8, incident_id="stale", opened_at=base)
        | {"resolved_at": datetime(2026, 4, 1, tzinfo=UTC)}
    )

    records = store.list_resolved_since(datetime(2026, 4, 15, tzinfo=UTC))
    assert [r.incident_id for r in records] == ["early", "late"]


def test_list_resolved_since_returns_empty_when_no_records_match():
    store, _, _ = _store()
    assert store.list_resolved_since(datetime(2026, 5, 1, tzinfo=UTC)) == []


def test_list_resolved_since_translates_atlas_exception():
    class _BoomCollection:
        def find(self, *_args, **_kwargs):
            raise RuntimeError("Atlas: connection reset")

    store, _, _ = _store(collection=_BoomCollection())  # type: ignore[arg-type]
    with pytest.raises(MemoryStoreUnavailable, match="resolved_at"):
        store.list_resolved_since(datetime(2026, 5, 1, tzinfo=UTC))


def test_list_active_few_shot_keys_reads_directory_stems(tmp_path):
    """Returns the stem of every ``.yaml`` file in the configured directory."""
    (tmp_path / "payment_db_pool_aaaa1111.yaml").write_text("a:\n", encoding="utf-8")
    (tmp_path / "checkout_deploy_bbbb2222.yaml").write_text("b:\n", encoding="utf-8")
    # Files with the wrong extension are ignored.
    (tmp_path / "README.md").write_text("# nope\n", encoding="utf-8")
    # Subdirectories are ignored.
    (tmp_path / "subdir").mkdir()

    cfg = MemoryStoreConfig(
        mongodb_uri="mongodb://x",
        database="db",
        collection="incidents",
        vector_index_name="incident_vec_idx",
        embedding_model_id="fake",
        embedding_dimensions=8,
        few_shot_directory=tmp_path,
    )
    store = MemoryStore(cfg)
    assert store.list_active_few_shot_keys() == {
        "payment_db_pool_aaaa1111",
        "checkout_deploy_bbbb2222",
    }


def test_list_active_few_shot_keys_returns_empty_when_directory_missing(tmp_path):
    cfg = MemoryStoreConfig(
        mongodb_uri="mongodb://x",
        database="db",
        collection="incidents",
        vector_index_name="incident_vec_idx",
        embedding_model_id="fake",
        embedding_dimensions=8,
        few_shot_directory=tmp_path / "does-not-exist",
    )
    store = MemoryStore(cfg)
    assert store.list_active_few_shot_keys() == set()


def test_few_shot_dir_defaults_to_in_package_directory():
    """When config doesn't override, point at the in-package _few_shot/ dir."""
    cfg = MemoryStoreConfig(
        mongodb_uri="mongodb://x",
        database="db",
        collection="incidents",
        vector_index_name="incident_vec_idx",
        embedding_model_id="fake",
        embedding_dimensions=8,
    )
    store = MemoryStore(cfg)
    resolved = store._few_shot_dir()
    assert resolved.name == "_few_shot"
    assert resolved.parent.name == "causal_oncall"
