"""ProblemSignature — the canonical, hashable description of a Dynatrace problem.

Hides: how a raw Dynatrace `problems` payload is normalized into a stable
signature suitable for embedding + vector-search lookup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime


def _parse_dynatrace_timestamp(raw: str) -> datetime:
    """Parse Dynatrace ISO-8601 strings (the trailing Z form) as UTC."""
    # Dynatrace emits e.g. "2026-05-17T09:30:00Z"; fromisoformat in 3.11+ accepts
    # the trailing "Z" only after we swap it to +00:00. Dynatrace always emits
    # tz-aware timestamps; the tz-naive branch is a defensive guard with no
    # production trigger, exercised by the dedicated unit test below.
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:  # pragma: no cover  # Dynatrace timestamps always carry tz info
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


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
        problem_id = str(payload["problemId"])
        title = str(payload["title"])
        severity = str(payload["severityLevel"])
        opened_at = _parse_dynatrace_timestamp(str(payload["startTime"]))

        ids: list[str] = []
        types: set[str] = set()
        for entity in payload.get("affectedEntities", ()):
            entity_id_field = entity.get("entityId", {}) or {}
            # Dynatrace always nests id under entityId.id for problem.open
            # payloads. We pull `type` from the top-level field with a
            # nested fallback so the unit fixture (top-level) and the live
            # Grail shape (nested) both produce the same signature.
            entity_id = entity_id_field.get("id", "")
            entity_type = entity.get("type") or entity_id_field.get("type", "")
            ids.append(str(entity_id))
            types.add(str(entity_type))
        # Drop empty placeholders that arise only when a payload omits the
        # corresponding field entirely; production payloads never do.
        ids = [i for i in ids if i]
        types = {t for t in types if t}

        sorted_ids = tuple(sorted(set(ids)))
        sorted_types = tuple(sorted(types))

        fingerprint_source = "|".join(
            (
                severity,
                title,
                ",".join(sorted_ids),
                ",".join(sorted_types),
            )
        )
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()[:16]

        return cls(
            problem_id=problem_id,
            title=title,
            severity=severity,
            affected_entity_ids=sorted_ids,
            affected_entity_types=sorted_types,
            opened_at=opened_at,
            fingerprint=fingerprint,
        )

    def to_embedding_text(self) -> str:
        """Render the signature as the text fed to the embedding model.

        The format is the contract: changing it invalidates every existing
        memory-store row, so it must be stable across deploys.
        """
        entity_types = ", ".join(self.affected_entity_types)
        return f"severity={self.severity}; title={self.title}; " f"entity_types={entity_types}"
