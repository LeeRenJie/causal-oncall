"""ProblemSignature — the canonical, hashable description of a Dynatrace problem.

Hides: how a raw Dynatrace `problems` payload is normalized into a stable
signature suitable for embedding + vector-search lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ProblemSignature:
    """A normalized, embedding-ready description of an open Dynatrace problem.

    Two distinct Dynatrace problem ids that describe "the same shape of
    incident" (same root entity type, same affected services, same fault
    category, comparable severity) should yield byte-identical signatures so
    that the memory store's vector search finds prior resolutions reliably.

    Attributes:
        problem_id: Dynatrace problem id (e.g. ``-1234567890123456789_v2``).
        title: Single-line human title surfaced by Dynatrace.
        severity: ``"AVAILABILITY" | "ERROR" | "PERFORMANCE" | "RESOURCE" | "CUSTOM"``.
        affected_entity_ids: Stable Dynatrace entity ids in deterministic order.
        affected_entity_types: ``("SERVICE", "PROCESS_GROUP", ...)`` deduped + sorted.
        opened_at: When Dynatrace opened the problem (UTC).
        fingerprint: Short, deterministic hash of the normalized payload.
            Used as a dedup key in the memory store.
    """

    problem_id: str
    title: str
    severity: str
    affected_entity_ids: tuple[str, ...]
    affected_entity_types: tuple[str, ...]
    opened_at: datetime
    fingerprint: str = field(default="")

    @classmethod
    def from_dynatrace_payload(cls, payload: dict) -> ProblemSignature:
        """Normalize a raw Dynatrace problem JSON into a ProblemSignature.

        The normalization must be idempotent: feeding the same payload
        twice (modulo timestamp jitter on opened_at) returns equal
        signatures, including the same fingerprint.
        """
        raise NotImplementedError(
            "Normalize a raw Dynatrace problem payload into a deterministic "
            "signature with a stable fingerprint."
        )

    def to_embedding_text(self) -> str:
        """Render the signature as the text fed to the embedding model.

        The format is the contract: changing it invalidates every existing
        memory-store row, so it must be stable across deploys.
        """
        raise NotImplementedError(
            "Produce the canonical embedding-input string for this signature."
        )
