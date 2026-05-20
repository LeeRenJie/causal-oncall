"""Seed the production Mongo Atlas memory store with the 10 pre-resolved fixtures.

Loads ``tests/fixtures/memory_seeds/seed_10_resolved.json``, generates
embeddings via Vertex AI ``text-embedding-005`` (768-dim), and upserts
each one into ``<MONGODB_DB>.incidents`` with the production document
shape so a freshly-deployed Cloud Run instance has a non-empty memory
corpus on day 1.

Usage (production)::

    # Populate .env with MONGODB_URI + GOOGLE_CLOUD_PROJECT + ADC, then:
    python scripts/seed_memory.py

The script is idempotent — the dedup key is
``(problem_signature_hash, brief_hash)`` per MemoryStore.record(), so
re-running it on an already-seeded DB is a safe no-op (touches
``updated_at`` only).

Per the W3-S1 contract: the Atlas vector index ``incident_vec_idx``
must already exist on the target collection (768-dim, cosine, path
``embedding``, filter ``confirmed_root_cause_key``). Create it once via
the Atlas UI before running this script.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path


def _seed() -> int:  # pragma: no cover  # human-driven script
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None  # type: ignore[assignment]

    if load_dotenv is not None:
        load_dotenv()

    mongodb_uri = os.environ.get("MONGODB_URI")
    if not mongodb_uri:
        print("FAIL: MONGODB_URI not set in environment", file=sys.stderr)
        return 1

    database = os.environ.get("MONGODB_DB", "causal_oncall")
    collection = os.environ.get("MONGODB_COLLECTION", "incidents")
    vector_index = os.environ.get("MONGODB_VECTOR_INDEX", "incident_vec_idx")
    embedding_model_id = os.environ.get("VERTEX_EMBEDDING_MODEL", "text-embedding-005")

    # Defer imports so the script's --help can run without GCP creds.
    from causal_oncall.domain.brief import Brief, Hypothesis
    from causal_oncall.domain.incident_record import IncidentRecord
    from causal_oncall.domain.problem_signature import ProblemSignature
    from causal_oncall.memory_store import MemoryStore, MemoryStoreConfig

    seed_path = (
        Path(__file__).resolve().parent.parent
        / "tests"
        / "fixtures"
        / "memory_seeds"
        / "seed_10_resolved.json"
    )
    raw = json.loads(seed_path.read_text(encoding="utf-8"))
    # W3-S3: seed JSON is now envelope-shaped {schema_version, records};
    # accept the legacy bare-list form too so external loaders aren't broken.
    seeds = raw["records"] if isinstance(raw, dict) else raw

    cfg = MemoryStoreConfig(
        mongodb_uri=mongodb_uri,
        database=database,
        collection=collection,
        vector_index_name=vector_index,
        embedding_model_id=embedding_model_id,
        embedding_dimensions=768,
        match_threshold=0.85,
    )
    store = MemoryStore(cfg)

    inserted = 0
    for seed in seeds:
        sig = ProblemSignature(
            problem_id=seed["incident_id"],
            title=seed["signature_title"],
            severity=seed["signature_severity"],
            affected_entity_ids=(seed["incident_id"] + "-svc",),
            affected_entity_types=tuple(seed["signature_entity_types"]),
            opened_at=datetime.fromisoformat(seed["opened_at"].replace("Z", "+00:00")),
            fingerprint=seed["fingerprint"],
        )
        hyp = Hypothesis(
            key=seed["confirmed_root_cause_key"],
            title=f"Prior incident: {seed['confirmed_root_cause_key']}",
            rank=1,
            score=1.0,
            supporting_evidence=(),
            refuting_evidence=(),
            next_action=seed["confirmed_fix"],
        )
        brief = Brief(
            problem_id=seed["incident_id"],
            generated_at=datetime.fromisoformat(seed["resolved_at"].replace("Z", "+00:00")),
            ranked_hypotheses=(hyp,),
            top_recommendation=seed["confirmed_fix"],
        )
        rec = IncidentRecord(
            incident_id=str(uuid.uuid5(uuid.NAMESPACE_DNS, seed["incident_id"])),
            signature=sig,
            brief=brief,
            opened_at=sig.opened_at,
            resolved_at=datetime.fromisoformat(seed["resolved_at"].replace("Z", "+00:00")),
            confirmed_root_cause_key=seed["confirmed_root_cause_key"],
            confirmed_fix=seed["confirmed_fix"],
        )
        store.record(rec)
        inserted += 1
        print(f"  seeded {seed['incident_id']}  ({seed['confirmed_root_cause_key']})")

    print(f"OK: {inserted} seed records upserted to {database}.{collection}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_seed())
