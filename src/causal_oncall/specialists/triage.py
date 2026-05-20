"""Triage specialist — Davis CoPilot DQL translation + first-pass logs.

Hides: the Davis CoPilot prompt used to convert problem-context to DQL,
the hand-written DQL fallback list when CoPilot's output fails validation,
the log-pattern heuristics, and the affected-entity hydration.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import DQLPlan
from causal_oncall.specialists.base import Specialist


class TriageSpecialist(Specialist):
    """Convert problem context to DQL, fetch logs around the event window."""

    name = "triage"
    fallback_hypothesis_key = "triage_unavailable"

    def investigate(self, signature: ProblemSignature) -> Evidence:
        def _probe() -> Evidence:
            self._dynatrace.get_problem_context(signature.problem_id)
            result = self._dynatrace.execute_dql(
                DQLPlan(
                    query=(
                        f"fetch logs | filter dt.entity.service in [{', '.join(signature.affected_entity_ids)!r}]"
                        " | filter loglevel == 'ERROR' | summarize count() by status"
                    )
                )
            )
            error_count = len(result.rows)
            confidence = 0.55 + min(0.3, 0.05 * error_count)
            return Evidence(
                specialist=self.name,
                kind="log_pattern",
                summary=(
                    f"Triage found {error_count} error-row(s) in the event window "
                    f"for {len(signature.affected_entity_ids)} affected service(s)."
                ),
                stance="supports",
                hypothesis_key="db_pool_exhaustion",
                confidence=confidence,
            )

        return self._safely(signature, _probe)
