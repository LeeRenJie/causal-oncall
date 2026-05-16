"""Brief + Hypothesis — what the synthesizer produces and what gets posted.

Hides: the Markdown rendering, Slack-block conversion, and Dynatrace-comment
formatting. Consumers see only typed objects, never strings of formatted text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

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
            "we've seen this 14x" wow moment in the brief header.
        unresolved_questions: Things the agent could not determine and that
            the on-call should answer to converge. Empty if fully decisive.
    """

    problem_id: str
    generated_at: datetime
    ranked_hypotheses: tuple[Hypothesis, ...]
    top_recommendation: str
    memory_short_circuit: bool = False
    unresolved_questions: tuple[str, ...] = field(default_factory=tuple)

    def to_markdown(self) -> str:
        """Render the brief as Markdown for the Dynatrace problem comment.

        The Slack notifier converts the same Brief to Slack block-kit; the
        two renderings share data but not formatting code.
        """
        raise NotImplementedError(
            "Render the brief as a Dynatrace-comment-compatible Markdown string."
        )
