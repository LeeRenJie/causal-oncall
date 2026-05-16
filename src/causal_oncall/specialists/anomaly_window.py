"""Anomaly-Window specialist — Davis Analyzers (forecast/changepoint/correlation).

Hides: which analyzers to run for which severity class, the metric
selection heuristic (prefer SLI golden-signals first), the changepoint
post-processing that promotes a deviation from "interesting" to
"causal candidate", and the correlation-coefficient threshold.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.specialists.base import Specialist


class AnomalyWindowSpecialist(Specialist):
    """Run forecast / changepoint / correlation analyzers across the anomaly window."""

    name = "anomaly_window"

    def investigate(self, signature: ProblemSignature) -> Evidence:
        raise NotImplementedError(
            "Anomaly window: run Davis changepoint + correlation analyzers across "
            "the SLI golden-signals and summarize the strongest deviation."
        )
