"""Contract tests for MemoryStore against a real Mongo Atlas test database.

Gated on ``MONGODB_URI`` being set to an Atlas connection string; CI
skips this suite. The purpose is to catch index-shape drift before
the demo, because mongomock cannot simulate ``$vectorSearch``.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.requires_creds,
    pytest.mark.skipif(
        not os.environ.get("MONGODB_URI"),
        reason="Mongo Atlas connection not set; contract suite gated on real env",
    ),
]


def test_match_returns_seeded_record_above_threshold():
    """Seed a known record, query for a near-duplicate signature, expect a Match."""
    raise NotImplementedError(
        "Seed one resolved IncidentRecord into the test DB, run match() with a "
        "minimally-perturbed signature, and assert a Match comes back with the "
        "expected confirmed_root_cause_key."
    )


def test_vector_index_exists_with_expected_dimensions():
    """The hand-created M0 vector index must match EMBEDDING_DIMENSIONS."""
    raise NotImplementedError(
        "Introspect the Atlas index metadata and assert the configured vector "
        "field dimensions match MemoryStoreConfig.embedding_dimensions."
    )
