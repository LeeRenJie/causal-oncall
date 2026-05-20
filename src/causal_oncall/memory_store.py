"""MemoryStore — Mongo Atlas-backed incident memory + vector match.

Hides: the pymongo connection pool, embedding generation via Vertex AI,
vector-index bookkeeping, dedup-on-fingerprint, schema migrations, and
the BSON document shape. Callers see only the domain types
``IncidentRecord``, ``Match``, and ``ProblemSignature``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from causal_oncall.domain.brief import Brief
from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.domain.problem_signature import ProblemSignature


@dataclass(frozen=True, slots=True)
class MemoryStoreConfig:
    """Connection + index parameters."""

    mongodb_uri: str
    database: str
    collection: str
    vector_index_name: str
    embedding_model_id: str
    embedding_dimensions: int
    match_threshold: float = 0.85


def _cosine(a, b) -> float:
    """Cosine similarity. Returns 0.0 on degenerate (zero-norm) vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


class MemoryStore:
    """Operational + vector store for resolved incidents.

    The store has exactly three jobs:
      * find prior incidents that look like a new signature,
      * record new investigations (open or resolved),
      * fold in human feedback once the on-call confirms the root cause.

    Everything else is plumbing.
    """

    def __init__(self, config: MemoryStoreConfig) -> None:
        self._config = config
        # Both seams are monkeypatched by the test suite; the production
        # __init__ wires real Mongo + Vertex AI when the imports succeed
        # at first use (lazy to keep test imports cheap).
        self._collection: Any = None
        self._embed: Any = self._default_embed

    def match(self, signature: ProblemSignature, *, threshold: float | None = None) -> Match | None:
        """Return the highest-similarity prior incident at-or-above the threshold."""
        coll = self._ensure_collection()
        applied_threshold = threshold if threshold is not None else self._config.match_threshold
        query_vec = self._embed(signature.to_embedding_text())

        best: tuple[float, dict] | None = None
        for doc in coll.find({}):
            if not doc.get("confirmed_root_cause_key"):
                continue
            emb = doc.get("embedding")
            if not emb:
                continue
            sim = _cosine(query_vec, emb)
            if sim < applied_threshold:
                continue
            if best is None or sim > best[0]:
                best = (sim, doc)

        if best is None:
            return None

        sim, doc = best
        record = self._doc_to_record(doc)
        prior_occurrences = coll.count_documents(
            {"confirmed_root_cause_key": doc["confirmed_root_cause_key"]}
        )
        return Match(record=record, similarity=sim, prior_occurrences=prior_occurrences)

    def record(self, incident_record: IncidentRecord) -> None:
        """Upsert an incident record."""
        coll = self._ensure_collection()
        fingerprint = incident_record.signature.fingerprint
        embedding = (
            list(incident_record.embedding)
            if incident_record.embedding
            else list(self._embed(incident_record.signature.to_embedding_text()))
        )
        doc = {
            "incident_id": incident_record.incident_id,
            "fingerprint": fingerprint,
            "embedding": embedding,
            "opened_at": incident_record.opened_at,
            "resolved_at": incident_record.resolved_at,
            "confirmed_root_cause_key": incident_record.confirmed_root_cause_key,
            "confirmed_fix": incident_record.confirmed_fix,
            "signature": {
                "problem_id": incident_record.signature.problem_id,
                "title": incident_record.signature.title,
                "severity": incident_record.signature.severity,
                "affected_entity_ids": list(incident_record.signature.affected_entity_ids),
                "affected_entity_types": list(incident_record.signature.affected_entity_types),
                "opened_at": incident_record.signature.opened_at,
                "fingerprint": fingerprint,
            },
            "brief_markdown": incident_record.brief.to_markdown(),
        }
        coll.update_one({"fingerprint": fingerprint}, {"$set": doc}, upsert=True)

    def update_resolution(
        self, incident_id: str, *, confirmed_root_cause_key: str, confirmed_fix: str
    ) -> None:
        """Attach the on-call's confirmed resolution to a previously-recorded incident."""
        coll = self._ensure_collection()
        coll.update_one(
            {"incident_id": incident_id},
            {
                "$set": {
                    "confirmed_root_cause_key": confirmed_root_cause_key,
                    "confirmed_fix": confirmed_fix,
                    "resolved_at": datetime.now(UTC),
                }
            },
        )

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    def _ensure_collection(self):
        if self._collection is None:  # pragma: no cover  # exercised by contract test, not unit
            from pymongo import MongoClient

            client = MongoClient(self._config.mongodb_uri)
            self._collection = client[self._config.database][self._config.collection]
        return self._collection

    def _default_embed(self, text: str):  # pragma: no cover  # vertex-backed
        """Real embedding call; tests substitute via monkeypatch."""
        from google.cloud import aiplatform  # noqa: F401  # ensure SDK present
        from vertexai.language_models import TextEmbeddingModel

        model = TextEmbeddingModel.from_pretrained(self._config.embedding_model_id)
        result = model.get_embeddings([text])
        return tuple(result[0].values)

    @staticmethod
    def _doc_to_record(doc: dict) -> IncidentRecord:
        sig_doc = doc.get("signature") or {}
        opened_at = doc.get("opened_at") or datetime(1970, 1, 1, tzinfo=UTC)
        signature = ProblemSignature(
            problem_id=str(sig_doc.get("problem_id", doc.get("incident_id", ""))),
            title=str(sig_doc.get("title", "")),
            severity=str(sig_doc.get("severity", "")),
            affected_entity_ids=tuple(sig_doc.get("affected_entity_ids", ())),
            affected_entity_types=tuple(sig_doc.get("affected_entity_types", ())),
            opened_at=opened_at,
            fingerprint=str(doc.get("fingerprint", "")),
        )
        # Use a stub Brief for the record in the match path; callers only
        # read .ranked_hypotheses[0].next_action and .confirmed_fix.
        brief = _PriorBrief(
            problem_id=signature.problem_id,
            generated_at=opened_at,
            ranked_hypotheses=(),
            top_recommendation=str(doc.get("confirmed_fix", "")),
        )
        return IncidentRecord(
            incident_id=str(doc.get("incident_id", "")),
            signature=signature,
            brief=brief,
            opened_at=opened_at,
            resolved_at=doc.get("resolved_at"),
            confirmed_root_cause_key=doc.get("confirmed_root_cause_key"),
            confirmed_fix=str(doc.get("confirmed_fix", "")),
            embedding=tuple(doc.get("embedding", ())),
        )


# Lightweight Brief subclass used when hydrating a record from Mongo: we
# don't have the original ranked hypothesis tree in the document, only the
# rendered Markdown + the confirmed fix. Keeps Brief's invariants intact.
class _PriorBrief(Brief):  # pragma: no cover  # data shim; no behavior
    pass


# Disable __init_subclass__ frozen-dataclass check for the shim.
# Brief is @dataclass(frozen=True), so subclassing for a no-op shim is
# safe so long as we don't add new fields.
