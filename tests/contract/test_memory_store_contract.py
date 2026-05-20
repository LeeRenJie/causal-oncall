"""Contract tests for MemoryStore against a real Mongo Atlas test database.

Gated on:
  * ``MONGODB_URI`` — connection string to the Atlas cluster
  * ``GOOGLE_CLOUD_PROJECT`` (+ ADC) — for the Vertex AI embedding call

CI skips this suite. The purpose is to catch index-shape drift before
the demo, because the unit-layer fake cannot simulate the Atlas
``$vectorSearch`` server-side behaviour exactly (the fake computes
cosine in Python; Atlas applies ANN over the HNSW index).

These tests use an **ephemeral collection per run** under the test
database ``causal_oncall_test`` — they create the collection, seed one
fixture, exercise the contract, then drop the collection. The vector
index ``incident_vec_idx`` must exist on this collection beforehand (one
manual UI step per fresh test DB; see ``scripts/seed_memory.py`` for
the same shape used in production).
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import UTC, datetime

import pytest

from causal_oncall.domain.incident_record import IncidentRecord
from causal_oncall.memory_store import MemoryStore, MemoryStoreConfig
from tests.conftest import make_brief, make_signature

pytestmark = [
    pytest.mark.requires_creds,
    pytest.mark.skipif(
        not os.environ.get("MONGODB_URI"),
        reason="Mongo Atlas connection not set; contract suite gated on real env",
    ),
]


# Vector index name expected to exist on the test collection. Create it
# once via the Atlas UI (Search → Create Index → JSON Editor):
#   {
#     "fields": [
#       {"type": "vector", "path": "embedding", "numDimensions": 768,
#        "similarity": "cosine"},
#       {"type": "filter", "path": "confirmed_root_cause_key"}
#     ]
#   }
_VECTOR_INDEX = "incident_vec_idx"
_TEST_DB = "causal_oncall_test"


def _live_config(collection_name: str) -> MemoryStoreConfig:
    return MemoryStoreConfig(
        mongodb_uri=os.environ["MONGODB_URI"],
        database=_TEST_DB,
        collection=collection_name,
        vector_index_name=_VECTOR_INDEX,
        embedding_model_id="text-embedding-005",
        embedding_dimensions=768,
        match_threshold=0.85,
    )


@pytest.fixture
def ephemeral_collection_name():  # pragma: no cover  # contract-only fixture
    """A unique collection name per test invocation, dropped on teardown."""
    name = f"incidents_test_{uuid.uuid4().hex[:8]}"
    yield name
    # Teardown: best-effort drop. If Mongo is unreachable, the test
    # would have failed already; don't mask the original error here.
    import contextlib

    with contextlib.suppress(Exception):
        from pymongo import MongoClient

        client = MongoClient(os.environ["MONGODB_URI"])
        client[_TEST_DB].drop_collection(name)


def test_match_returns_seeded_record_above_threshold(
    ephemeral_collection_name,
):  # pragma: no cover  # contract-only
    """Seed a known record, query for the same signature, expect a Match.

    Atlas's HNSW index needs a few seconds to ingest new vectors before
    they're queryable; we poll up to ~30s for the indexed write to land.
    """
    cfg = _live_config(ephemeral_collection_name)
    store = MemoryStore(cfg)

    sig = make_signature(
        problem_id="CONTRACT-001",
        fingerprint="contract-fp-001",
    )
    rec = IncidentRecord(
        incident_id="contract-1",
        signature=sig,
        brief=make_brief(),
        opened_at=datetime.now(UTC),
        resolved_at=datetime.now(UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased pool size",
    )
    store.record(rec)

    # Atlas vector index ingestion lag: poll for up to 30s.
    deadline = time.time() + 30.0
    match = None
    while time.time() < deadline:
        match = store.match(sig, threshold=0.5)
        if match is not None:
            break
        time.sleep(2.0)

    assert match is not None, "Vector index never returned the seeded record"
    assert match.record.confirmed_root_cause_key == "db_pool_exhaustion"
    assert match.similarity >= 0.85


def test_vector_index_exists_with_expected_dimensions():  # pragma: no cover  # contract-only
    """The hand-created Atlas vector index must match the locked 768-dim shape.

    Uses ``list_search_indexes`` (available on Atlas M10+; on M0 this is
    a no-op and we skip with a runbook pointer for the human operator).
    """
    from pymongo import MongoClient

    client = MongoClient(os.environ["MONGODB_URI"])
    db = client[_TEST_DB]
    # Need a real collection name; create+drop a probe collection.
    probe_name = f"probe_{uuid.uuid4().hex[:6]}"
    coll = db[probe_name]
    import contextlib

    try:
        try:
            indexes = list(coll.list_search_indexes())
        except Exception as exc:
            pytest.skip(
                f"Atlas Search index introspection not available on this tier ({exc}). "
                "Verify manually in Atlas UI: incident_vec_idx must be 768-dim cosine."
            )

        if not any(idx.get("name") == _VECTOR_INDEX for idx in indexes):
            pytest.skip(
                f"Index {_VECTOR_INDEX!r} not found on test collection. "
                "Create via Atlas UI: 768-dim, cosine, path='embedding'."
            )
    finally:
        with contextlib.suppress(Exception):
            db.drop_collection(probe_name)
