"""Triage specialist — Davis CoPilot DQL translation + first-pass logs.

Hides: the Davis CoPilot prompt used to convert problem-context to DQL,
the hand-written DQL fallback list when CoPilot's output fails validation,
the log-pattern heuristics, and the affected-entity hydration.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.specialists.base import Specialist


class TriageSpecialist(Specialist):
    """Convert problem context to DQL, fetch logs around the event window."""

    name = "triage"

    def investigate(self, signature: ProblemSignature) -> Evidence:
        raise NotImplementedError(
            "Triage: translate signature to DQL via Davis CoPilot (with fallback "
            "templates), execute, and summarize log-pattern findings as Evidence."
        )
