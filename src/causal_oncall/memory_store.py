"""MemoryStore — Mongo Atlas-backed incident memory + vector match.

Hides: the pymongo connection pool, embedding generation via Vertex AI
(`text-embedding-005`, 768-dim), Atlas ``$vectorSearch`` aggregation
against the ``incident_vec_idx`` cosine index, dedup-on-fingerprint +
brief-hash, schema migrations, the BSON document shape, and the
``datetime.now(UTC)`` timestamping. Callers see only the domain types
``IncidentRecord``, ``Match``, and ``ProblemSignature``.

The match path runs Atlas ``$vectorSearch`` on the production database
(index ``incident_vec_idx``, cosine, 768-dim) and returns the top hit
when its score meets or exceeds the configured threshold (default 0.85
per UNIQUE_IDEA's "high confidence ≥0.85 → short-circuit"). Unit tests
inject a fake collection whose ``.aggregate()`` simulates the same
shape; the contract suite under ``tests/contract/`` exercises the live
Atlas path against an ephemeral test database.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from causal_oncall.domain.brief import Brief
from causal_oncall.domain.exceptions import MemoryStoreUnavailable
from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.domain.problem_signature import ProblemSignature


@dataclass(frozen=True, slots=True)
class MemoryStoreConfig:
    """Connection + index parameters.

    Read from environment by the production wiring; tests construct
    directly with the values the fake fixtures expect. ``mongodb_uri``
    must come from the operator's environment (never hardcoded) so
    spike-only TLS workarounds (``tlsInsecure=true``) stay out of code.
    """

    mongodb_uri: str
    database: str
    collection: str
    vector_index_name: str
    embedding_model_id: str
    embedding_dimensions: int
    match_threshold: float = 0.85
    vector_search_num_candidates: int = 100


# Type alias: the embedder callable takes the signature text and returns
# a 768-dim float vector. Kept narrow so the test fake is a one-liner.
Embedder = Callable[[str], Iterable[float]]


def _cosine(a: Iterable[float], b: Iterable[float]) -> float:
    """Cosine similarity. Returns 0.0 on degenerate (zero-norm) vectors."""
    a_list = list(a)
    b_list = list(b)
    dot = sum(x * y for x, y in zip(a_list, b_list, strict=False))
    na = math.sqrt(sum(x * x for x in a_list))
    nb = math.sqrt(sum(y * y for y in b_list))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _hash_text(text: str) -> str:
    """Stable 16-char hex digest, used for dedup keys (signature + brief)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class MemoryStore:
    """Operational + vector store for resolved incidents.

    The store has exactly three jobs (locked, deep-module surface):
      * :meth:`match` — find the prior incident that looks most like a new
        signature; return ``None`` below the threshold.
      * :meth:`record` — upsert an investigation, deduped on the
        ``(problem_signature_hash, brief_hash)`` pair so retries of the
        same brief on the same incident don't blow up the corpus.
      * :meth:`update_resolution` — fold the on-call's confirmed verdict
        back into the row once Slack feedback arrives.

    Everything else (pymongo client, Vertex AI embedding model, vector
    index management, schema validation, dedup, ``$vectorSearch`` shape)
    is plumbing hidden behind these three.
    """

    def __init__(
        self,
        config: MemoryStoreConfig,
        *,
        embedder: Embedder | None = None,
        collection: Any | None = None,
    ) -> None:
        self._config = config
        # Both seams are dependency-injectable: production wiring passes
        # nothing (lazy-imports the real Vertex AI + pymongo clients on
        # first use), tests inject fakes through the keyword args. Direct
        # attribute monkeypatching also works because the seams are
        # accessed via attribute lookup, not closure capture.
        self._collection: Any = collection
        self._embed: Embedder = embedder if embedder is not None else self._default_embed

    # ------------------------------------------------------------------ #
    # Public surface — three methods. Adding a fourth requires a PLAN
    # amendment per the deep-module rule.
    # ------------------------------------------------------------------ #

    def match(
        self,
        signature: ProblemSignature,
        *,
        threshold: float | None = None,
    ) -> Match | None:
        """Return the highest-similarity prior incident at-or-above the threshold.

        Wraps Atlas ``$vectorSearch`` against the configured vector index.
        On production this is a single round-trip; on a fake collection
        the same pipeline produces the same shape via Python-side cosine.
        """
        coll = self._ensure_collection()
        applied_threshold = threshold if threshold is not None else self._config.match_threshold
        query_vec = list(self._embed(signature.to_embedding_text()))

        pipeline = [
            {
                "$vectorSearch": {
                    "index": self._config.vector_index_name,
                    "path": "embedding",
                    "queryVector": query_vec,
                    "numCandidates": self._config.vector_search_num_candidates,
                    "limit": 10,
                    "filter": {"confirmed_root_cause_key": {"$exists": True, "$ne": None}},
                }
            },
            {
                "$addFields": {
                    "score": {"$meta": "vectorSearchScore"},
                }
            },
        ]

        try:
            hits = list(coll.aggregate(pipeline))
        except Exception as exc:
            raise MemoryStoreUnavailable(
                f"Atlas $vectorSearch failed against index "
                f"{self._config.vector_index_name!r}: {exc}"
            ) from exc

        best: tuple[float, dict] | None = None
        for doc in hits:
            score = float(doc.get("score", 0.0))
            if score < applied_threshold:
                continue
            if best is None or score > best[0]:
                best = (score, doc)

        if best is None:
            return None

        sim, doc = best
        record = self._doc_to_record(doc)
        prior_occurrences = coll.count_documents(
            {"confirmed_root_cause_key": doc.get("confirmed_root_cause_key")}
        )
        return Match(record=record, similarity=sim, prior_occurrences=prior_occurrences)

    def record(self, incident_record: IncidentRecord) -> None:
        """Upsert an incident record, deduped on (signature_hash, brief_hash).

        Same problem + same rendered brief = update timestamps + return.
        Same problem + different brief (e.g. specialists found new
        evidence) = a fresh row, preserving history.
        """
        coll = self._ensure_collection()
        signature_hash = incident_record.signature.fingerprint
        brief_md = incident_record.brief.to_markdown()
        brief_hash = _hash_text(brief_md)

        embedding = (
            list(incident_record.embedding)
            if incident_record.embedding
            else list(self._embed(incident_record.signature.to_embedding_text()))
        )

        now = datetime.now(UTC)
        doc = {
            "incident_id": incident_record.incident_id,
            "problem_signature_hash": signature_hash,
            "brief_hash": brief_hash,
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
                "fingerprint": signature_hash,
            },
            "brief_markdown": brief_md,
            "updated_at": now,
        }
        # ``$setOnInsert`` keeps ``created_at`` stable across re-records of
        # the same (signature, brief) pair while ``$set`` refreshes
        # everything else. Upsert key is the dedup pair.
        try:
            coll.update_one(
                {
                    "problem_signature_hash": signature_hash,
                    "brief_hash": brief_hash,
                },
                {"$set": doc, "$setOnInsert": {"created_at": now}},
                upsert=True,
            )
        except Exception as exc:
            raise MemoryStoreUnavailable(f"Atlas upsert failed: {exc}") from exc

    def update_resolution(
        self,
        incident_id: str,
        *,
        confirmed_root_cause_key: str,
        confirmed_fix: str,
    ) -> None:
        """Attach the on-call's confirmed resolution to a previously-recorded incident.

        Called by the Slack feedback handler (W2-S3) once the human
        clicks the "was hypothesis #N the real fix?" button.
        """
        coll = self._ensure_collection()
        try:
            coll.update_one(
                {"incident_id": incident_id},
                {
                    "$set": {
                        "confirmed_root_cause_key": confirmed_root_cause_key,
                        "confirmed_fix": confirmed_fix,
                        "resolved_at": datetime.now(UTC),
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
        except Exception as exc:
            raise MemoryStoreUnavailable(f"Atlas update failed: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Internals — everything below is hidden plumbing.
    # ------------------------------------------------------------------ #

    def _ensure_collection(self):
        if self._collection is None:  # pragma: no cover  # exercised by contract test only
            from pymongo import MongoClient

            client = MongoClient(self._config.mongodb_uri)
            self._collection = client[self._config.database][self._config.collection]
        return self._collection

    def _default_embed(self, text: str):  # pragma: no cover  # Vertex-backed; contract-only
        """Real embedding call via Vertex AI ``text-embedding-005`` (768-dim).

        Tests substitute by passing ``embedder=`` to the constructor or
        monkeypatching ``store._embed``. Lazy-imported so unit tests
        never pay the Vertex AI SDK import cost.
        """
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
            fingerprint=str(doc.get("problem_signature_hash", sig_doc.get("fingerprint", ""))),
        )
        # Use a lightweight Brief shim for the record in the match path:
        # callers read .ranked_hypotheses[0].next_action or .confirmed_fix.
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
