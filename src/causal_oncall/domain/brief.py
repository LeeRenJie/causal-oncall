"""Brief + Hypothesis — what the synthesizer produces and what gets posted.

Hides: the Markdown rendering, Slack-block conversion, and Dynatrace-comment
formatting. Consumers see only typed objects, never strings of formatted text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from causal_oncall.domain.evidence import Evidence


@dataclass(frozen=True, slots=True)
class Hypothesis:
    """One ranked candidate root cause.

    Attributes:
        key: Stable short id matching the hypothesis_key on supporting Evidence.
        title: Human-facing label, e.g. "DB connection pool exhausted by deploy v412".
        rank: 1-indexed position in the brief; 1 = the agent's top guess.
        score: 0.0–1.0 composite score. Higher = stronger total evidence weight.
        supporting_evidence: Evidence whose stance is ``"supports"``.
        refuting_evidence: Evidence whose stance is ``"refutes"``.
        next_action: One-sentence recommendation the on-call can act on
            immediately if they accept this hypothesis.
    """

    key: str
    title: str
    rank: int
    score: float
    supporting_evidence: tuple[Evidence, ...]
    refuting_evidence: tuple[Evidence, ...]
    next_action: str


@dataclass(frozen=True, slots=True)
class Brief:
    """The full incident brief posted to Slack and as a Dynatrace comment.

    Attributes:
        problem_id: Dynatrace problem id this brief diagnoses.
        generated_at: UTC timestamp the brief was composed.
        ranked_hypotheses: 1..N hypotheses, sorted by rank ascending.
        top_recommendation: Pulled from ``ranked_hypotheses[0].next_action``
            for fast scanning in the Slack notification preview.
        memory_short_circuit: True iff the orchestrator hit a high-confidence
            memory match and skipped specialist dispatch. Surfaces the
            "we've seen this 14x" wow moment in the brief header. Mirrors
            ``from_memory`` for the existing SSE consumer + tests; new
            consumers should prefer ``from_memory`` + ``pattern_match_score``.
        unresolved_questions: Things the agent could not determine and that
            the on-call should answer to converge. Empty if fully decisive.
        from_memory: True iff this brief was built from a prior
            IncidentRecord rather than a fresh specialist run. The W3-S2
            pre-flight short-circuit sets this on high-confidence matches.
        pattern_match_score: When ``from_memory`` is True, the cosine
            similarity score that triggered the short-circuit (0.0–1.0).
            ``None`` on fresh investigations.

    The class-level :pyattr:`SCHEMA_VERSION` constant is bumped each time
    a persisted-shape change ships; W3-S2 added ``from_memory`` +
    ``pattern_match_score`` and bumped to ``2``.
    """

    #: Bump on every change to the persisted Brief shape. Read by the
    #: Curator + MemoryStore migration code, and surfaced in the brief
    #: footer for diagnostic traceability across cassette/seed re-records.
    #: ``ClassVar`` keeps it off the dataclass-field list, so it does not
    #: appear in ``__init__`` or ``__eq__``.
    SCHEMA_VERSION: ClassVar[int] = 2

    problem_id: str
    generated_at: datetime
    ranked_hypotheses: tuple[Hypothesis, ...]
    top_recommendation: str
    memory_short_circuit: bool = False
    unresolved_questions: tuple[str, ...] = field(default_factory=tuple)
    from_memory: bool = False
    pattern_match_score: float | None = None

    def __post_init__(self) -> None:
        # Invariant: if a pattern_match_score is present, from_memory must
        # be True; conversely, from_memory without a score is also rejected
        # so the "we've seen this 14x" badge always carries its evidence.
        # Frozen dataclass — no field assignment, only validation.
        if self.pattern_match_score is not None and not (0.0 <= self.pattern_match_score <= 1.0):
            raise ValueError(
                f"pattern_match_score must be in [0.0, 1.0]; got {self.pattern_match_score!r}"
            )
        if self.from_memory and self.pattern_match_score is None:
            raise ValueError("Brief.from_memory=True requires pattern_match_score to be set")
        if self.pattern_match_score is not None and not self.from_memory:
            raise ValueError("Brief.pattern_match_score is only valid when from_memory=True")

    def to_markdown(self) -> str:
        """Render the brief as Markdown for the Dynatrace problem comment.

        The Slack notifier converts the same Brief to Slack block-kit; the
        two renderings share data but not formatting code.
        """
        lines: list[str] = []
        lines.append(f"# Causal On-Call brief for {self.problem_id}")
        lines.append("")
        if self.memory_short_circuit:
            lines.append(
                "> **Memory hit** — we have seen this incident shape before; "
                "the recommendation below is the proven prior fix."
            )
            lines.append("")
        lines.append(f"**Next action:** {self.top_recommendation}")
        lines.append("")
        lines.append("## Ranked hypotheses")
        ranked = sorted(self.ranked_hypotheses, key=lambda h: h.rank)
        for hyp in ranked:
            lines.append("")
            lines.append(f"### {hyp.rank}. {hyp.title}")
            lines.append(f"_score: {hyp.score:.2f}_")
            lines.append("")
            lines.append(f"**Recommended action:** {hyp.next_action}")
            if hyp.supporting_evidence:
                lines.append("")
                lines.append("**Supporting evidence:**")
                for ev in hyp.supporting_evidence:
                    bullet = f"- ({ev.specialist}, conf={ev.confidence:.2f}) {ev.summary}"
                    lines.append(bullet)
                    for link in ev.dynatrace_links:
                        lines.append(f"  - [Open in Dynatrace]({link})")
            if hyp.refuting_evidence:
                lines.append("")
                lines.append("**Refuting evidence:**")
                for ev in hyp.refuting_evidence:
                    lines.append(f"- ({ev.specialist}, conf={ev.confidence:.2f}) {ev.summary}")
        if self.unresolved_questions:
            lines.append("")
            lines.append("## Open questions for the on-call")
            for q in self.unresolved_questions:
                lines.append(f"- {q}")
        lines.append("")
        lines.append(
            f"_Generated at {self.generated_at.isoformat()} "
            f"(brief schema v{self.SCHEMA_VERSION})_"
        )
        return "\n".join(lines)
