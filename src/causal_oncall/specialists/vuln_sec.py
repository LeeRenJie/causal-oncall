"""Vuln/Sec specialist — vulnerability-as-root-cause check.

Hides: the security-problem DQL filter (newly-active vulns only), the
CVE-to-affected-entity join, and the under-investigated "deploy
introduced a CVE that opened the door" pattern detection.
"""

from __future__ import annotations

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.specialists.base import Specialist


class VulnSecSpecialist(Specialist):
    """Check whether any security problem opened in the same window."""

    name = "vuln_sec"

    def investigate(self, signature: ProblemSignature) -> Evidence:
        raise NotImplementedError(
            "Vuln/Sec: query newly-active security problems whose affected "
            "entities overlap with the incident, and summarize as Evidence."
        )
