"""Deploy-Correlation specialist — was a deploy the trigger?

Hides: the deploy-events DQL, the event-window-vs-anomaly-window
alignment math, the commit-metadata lookup (best-effort, optional),
and the confidence scaling based on time-since-deploy.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.specialists.base import Specialist


class DeployCorrelationSpecialist(Specialist):
    """Find deploys that fall inside the problem's anomaly window."""

    name = "deploy_correlation"

    def investigate(self, signature: ProblemSignature) -> Evidence:
        raise NotImplementedError(
            "Deploy correlation: query deploy events in the anomaly window for "
            "the affected entities and summarize the most-likely culprit deploy."
        )
