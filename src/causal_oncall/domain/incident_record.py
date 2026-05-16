"""IncidentRecord + Match — the memory store's row + query result types.

Hides: the Mongo document shape, the BSON field names, the vector index
dimensions. Callers reason in terms of "did we see this before, with what
confidence, and what was the human-confirmed fix".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from causal_oncall.domain.brief import Brief
from causal_oncall.domain.problem_signature import ProblemSignature


@dataclass(frozen=True, slots=True)
class IncidentRecord:
    """One row in the incident memory store.

    Attributes:
        incident_id: Stable, agent-generated id (UUIDv7-shaped).
        signature: The ProblemSignature that triggered the original investigation.
        brief: The full Brief the agent generated at the time.
        opened_at: When the originating Dynatrace problem opened.
        resolved_at: When the on-call confirmed resolution; None while open.
        confirmed_root_cause_key: Hypothesis key the on-call confirmed was
            correct. None until human feedback arrives.
        confirmed_fix: Free-text fix description recorded by the on-call.
        embedding: 768-dim vector of the signature; persisted with the row
            so re-indexing doesn't require regeneration.
    """

    incident_id: str
    signature: ProblemSignature
    brief: Brief
    opened_at: datetime
    resolved_at: datetime | None = None
    confirmed_root_cause_key: str | None = None
    confirmed_fix: str = ""
    embedding: tuple[float, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class Match:
    """Result of MemoryStore.match() when a prior record meets the threshold.

    Attributes:
        record: The matched IncidentRecord.
        similarity: Cosine similarity 0.0–1.0 to the query signature.
        prior_occurrences: How many other resolved records share this match's
            ``confirmed_root_cause_key``. Drives the "seen this 14x" badge.
    """

    record: IncidentRecord
    similarity: float
    prior_occurrences: int
