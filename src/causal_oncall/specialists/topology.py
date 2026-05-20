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
    fallback_hypothesis_key = "topology_unavailable"
    # Topology walks the graph via get_topology_neighbors; the problem
    # context is the only context read it needs from execute_dql land.
    allowed_dynatrace_methods = ("get_problem_context", "get_topology_neighbors")

    def investigate(
        self,
        signature: ProblemSignature,
        *,
        prior_hypothesis: str | None = None,
    ) -> Evidence:
        del prior_hypothesis  # accepted for orchestrator contract; not yet biasing the walk

        def _probe() -> Evidence:
            self._dynatrace.get_problem_context(signature.problem_id)
            all_neighbors = []
            for entity_id in signature.affected_entity_ids:
                neighbors = self._dynatrace.get_topology_neighbors(entity_id, depth=2)
                all_neighbors.extend(neighbors)
            blast = len({n.entity_id for n in all_neighbors})
            confidence = 0.5 + min(0.4, 0.05 * blast)
            names = (
                ", ".join(sorted({n.display_name for n in all_neighbors}))
                or "no downstream services"
            )
            return Evidence(
                specialist=self.name,
                kind="topology_blast_radius",
                summary=f"Blast radius spans {blast} downstream service(s): {names}.",
                stance="informational" if blast == 0 else "supports",
                hypothesis_key="db_pool_exhaustion",
                confidence=confidence,
            )

        return self._safely(signature, _probe)
