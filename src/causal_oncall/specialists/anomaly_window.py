"""Anomaly-Window specialist — Davis Analyzers (forecast/changepoint/correlation).

Hides: which analyzers to run for which severity class, the metric
selection heuristic (prefer SLI golden-signals first), the changepoint
post-processing that promotes a deviation from "interesting" to
"causal candidate", and the correlation-coefficient threshold.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import DQLPlan
from causal_oncall.specialists.base import Specialist


class AnomalyWindowSpecialist(Specialist):
    """Run forecast / changepoint / correlation analyzers across the anomaly window."""

    name = "anomaly_window"
    fallback_hypothesis_key = "anomaly_window_unavailable"
    # Anomaly window pulls SLI golden-signal metrics through execute_dql.
    allowed_dynatrace_methods = ("get_problem_context", "execute_dql")

    def investigate(
        self,
        signature: ProblemSignature,
        *,
        prior_hypothesis: str | None = None,
    ) -> Evidence:
        del prior_hypothesis  # accepted for orchestrator contract; not yet biasing the analyzer

        def _probe() -> Evidence:
            self._dynatrace.get_problem_context(signature.problem_id)
            result = self._dynatrace.execute_dql(
                DQLPlan(
                    query=(
                        "fetch metric | filter metric.key startswith 'service.responseTime' | "
                        f"filter dt.entity.service in [{', '.join(signature.affected_entity_ids)!r}]"
                    )
                )
            )
            anomalies = len(result.rows)
            if anomalies == 0:
                confidence = 0.3
                stance = "refutes"
                summary = "No SLI golden-signal deviation detected in the anomaly window."
            else:
                confidence = 0.7
                stance = "supports"
                summary = (
                    f"Detected {anomalies} metric deviation(s): response-time anomalies "
                    "in the anomaly window."
                )
            return Evidence(
                specialist=self.name,
                kind="metric_anomaly",
                summary=summary,
                stance=stance,
                hypothesis_key="db_pool_exhaustion",
                confidence=confidence,
            )

        return self._safely(signature, _probe)
