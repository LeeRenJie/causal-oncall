"""Topology specialist — dependency walk + blast-radius ranking.

Hides: the topology traversal depth heuristic, the downstream-service
risk scoring (criticality * health-trend * fan-out), and the
deduplication of overlapping subgraphs.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.specialists.base import Specialist


class TopologySpecialist(Specialist):
    """Walk the topology outward, rank downstream services by blast radius."""

    name = "topology"

    def investigate(self, signature: ProblemSignature) -> Evidence:
        raise NotImplementedError(
            "Topology: traverse downstream neighbors of affected entities and "
            "summarize the top-N at-risk services as Evidence."
        )
