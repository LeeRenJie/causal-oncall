"""Vuln/Sec specialist — vulnerability-as-root-cause check.

Hides: the security-problem DQL filter (newly-active vulns only), the
CVE-to-affected-entity join, and the under-investigated "deploy
introduced a CVE that opened the door" pattern detection.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import DQLPlan
from causal_oncall.specialists.base import Specialist


class VulnSecSpecialist(Specialist):
    """Check whether any security problem opened in the same window."""

    name = "vuln_sec"
    fallback_hypothesis_key = "vuln_sec_unavailable"
    # Vuln/sec scans the security.events stream via execute_dql.
    allowed_dynatrace_methods = ("get_problem_context", "execute_dql")

    def investigate(
        self,
        signature: ProblemSignature,
        *,
        prior_hypothesis: str | None = None,
    ) -> Evidence:
        del prior_hypothesis  # accepted for orchestrator contract; not yet biasing the DQL

        def _probe() -> Evidence:
            self._dynatrace.get_problem_context(signature.problem_id)
            result = self._dynatrace.execute_dql(
                DQLPlan(
                    query=(
                        "fetch security.events | filter event.kind == 'NEW_VULN' | "
                        f"filter dt.entity.service in [{', '.join(signature.affected_entity_ids)!r}]"
                    )
                )
            )
            cves = len(result.rows)
            if cves == 0:
                confidence = 0.25
                stance = "refutes"
                summary = "No newly-active vulnerabilities overlap the incident window."
            else:
                confidence = 0.65
                stance = "supports"
                summary = f"Found {cves} newly-active CVE(s) overlapping the incident window."
            return Evidence(
                specialist=self.name,
                kind="vulnerability",
                summary=summary,
                stance=stance,
                hypothesis_key="cve_exposure",
                confidence=confidence,
            )

        return self._safely(signature, _probe)
