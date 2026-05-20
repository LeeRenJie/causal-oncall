"""Evidence — the structured output every specialist returns.

Hides: the specialist-specific raw artifacts (DQL result tables, topology
graphs, deploy events) behind one uniform "supports / refutes which
hypothesis with what confidence" envelope the synthesizer can rank.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

EvidenceKind = Literal[
    "metric_anomaly",
    "deploy_correlation",
    "topology_blast_radius",
    "log_pattern",
    "vulnerability",
    "memory_pattern",
]

Stance = Literal["supports", "refutes", "informational"]


@dataclass(frozen=True, slots=True)
class Evidence:
    """One structured finding produced by one specialist.

    Attributes:
        specialist: Name of the specialist that produced this evidence
            (e.g. ``"deploy_correlation"``). Used for provenance in the brief.
        kind: Category of the finding. Drives how the synthesizer renders
            it and how the Phoenix tracer tags the span.
        summary: Single-sentence English summary surfaced verbatim in the brief.
        stance: Whether this evidence ``supports``, ``refutes``, or is purely
            ``informational`` for the hypothesis it attaches to.
        hypothesis_key: A short stable key (e.g. ``"db_pool_exhaustion"``) that
            lets the synthesizer aggregate evidence per candidate hypothesis.
        confidence: 0.0–1.0 specialist-self-assessed confidence in this finding.
        dynatrace_links: Deep links back into the Dynatrace UI for the
            on-call to drill into the underlying data.
        raw_payload: Specialist-specific raw artifact preserved for the
            Phoenix trace; never read by the synthesizer.
    """

    specialist: str
    kind: EvidenceKind
    summary: str
    stance: Stance
    hypothesis_key: str
    confidence: float
    dynatrace_links: tuple[str, ...] = field(default_factory=tuple)
    raw_payload: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Reject malformed evidence at construction time.

        Confidence outside [0.0, 1.0] is a programming error in a
        specialist and must be caught before the synthesizer trusts it
        for ranking. Empty summaries make for a useless brief.
        """
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"Evidence.confidence must be within [0.0, 1.0]; got {self.confidence!r}"
            )
        if not self.summary:
            raise ValueError("Evidence.summary must be non-empty")
        if not self.hypothesis_key:
            raise ValueError("Evidence.hypothesis_key must be non-empty")

    def __hash__(self) -> int:
        """Hash on the identity fields only.

        ``raw_payload`` is a mutable dict provenance artifact for the
        Phoenix trace, never relied on for equality / set-membership
        semantics. Excluding it keeps Evidence hashable while preserving
        the frozen-dataclass contract for the rest of the fields.
        """
        return hash(
            (
                self.specialist,
                self.kind,
                self.summary,
                self.stance,
                self.hypothesis_key,
                self.confidence,
                self.dynatrace_links,
            )
        )
