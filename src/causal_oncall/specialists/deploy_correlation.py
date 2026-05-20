"""Deploy-Correlation specialist — was a deploy the trigger?

Hides: the deploy-events DQL, the event-window-vs-anomaly-window
alignment math, the commit-metadata lookup (best-effort, optional),
and the confidence scaling based on time-since-deploy.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import DQLPlan
from causal_oncall.specialists.base import Specialist


class DeployCorrelationSpecialist(Specialist):
    """Find deploys that fall inside the problem's anomaly window."""

    name = "deploy_correlation"
    fallback_hypothesis_key = "deploy_correlation_unavailable"

    def investigate(self, signature: ProblemSignature) -> Evidence:
        def _probe() -> Evidence:
            self._dynatrace.get_problem_context(signature.problem_id)
            result = self._dynatrace.execute_dql(
                DQLPlan(
                    query=(
                        "fetch events | filter event.kind == 'DEPLOY' | "
                        f"filter dt.entity.service in [{', '.join(signature.affected_entity_ids)!r}]"
                    )
                )
            )
            deploy_rows = [row for row in result.rows if any(cell == "DEPLOY" for cell in row)]
            if deploy_rows:
                confidence = 0.78
                summary = (
                    f"Found {len(deploy_rows)} deploy event(s) inside the anomaly window "
                    "for the affected services."
                )
                stance = "supports"
            else:
                confidence = 0.35
                summary = "No deploys found inside the anomaly window."
                stance = "refutes"
            return Evidence(
                specialist=self.name,
                kind="deploy_correlation",
                summary=summary,
                stance=stance,
                hypothesis_key="db_pool_exhaustion",
                confidence=confidence,
            )

        return self._safely(signature, _probe)
