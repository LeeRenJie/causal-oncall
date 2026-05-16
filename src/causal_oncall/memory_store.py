"""MemoryStore — Mongo Atlas-backed incident memory + vector match.

Hides: the pymongo connection pool, embedding generation via Vertex AI,
vector-index bookkeeping, dedup-on-fingerprint, schema migrations, and
the BSON document shape. Callers see only the domain types
``IncidentRecord``, ``Match``, and ``ProblemSignature``.
"""

from __future__ import annotations

from dataclasses import dataclass

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

    def match(
        self, signature: ProblemSignature, *, threshold: float | None = None
    ) -> Match | None:
        """Return the highest-similarity prior incident at-or-above the threshold.

        Embeds the signature, runs Atlas Vector Search over the collection,
        and returns ``None`` if no candidate meets ``threshold`` (defaults
        to the per-store config value). Never returns a record whose
        ``confirmed_root_cause_key`` is None.
        """
        raise NotImplementedError(
            "Embed the signature, run Atlas $vectorSearch, and return the top "
            "above-threshold Match (with prior_occurrences populated) or None."
        )

    def record(self, incident_record: IncidentRecord) -> None:
        """Upsert an incident record.

        Dedup is keyed on ``signature.fingerprint``: writing the same
        fingerprint twice updates the existing row rather than creating
        a duplicate.
        """
        raise NotImplementedError(
            "Upsert the incident record by signature.fingerprint, generating "
            "and persisting the embedding if not already present."
        )

    def update_resolution(
        self, incident_id: str, *, confirmed_root_cause_key: str, confirmed_fix: str
    ) -> None:
        """Attach the on-call's confirmed resolution to a previously-recorded incident.

        This is the only path by which a record becomes eligible to be
        returned from ``match()``; an incident with no confirmed root
        cause is treated as "still open" and is never a memory hit.
        """
        raise NotImplementedError(
            "Update the incident's resolved_at, confirmed_root_cause_key, and "
            "confirmed_fix fields atomically."
        )
