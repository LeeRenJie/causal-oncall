"""Specialist ABC — the one contract every sub-agent honors.

Hides: nothing in itself. Defines the seam by which the orchestrator
can dispatch specialists without knowing their internals, and by which
new specialists can be added without changing the orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.exceptions import (
    DynatraceUnavailable,
    RateLimited,
)
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import DynatraceClient


class Specialist(ABC):
    """One narrow investigator.

    Subclasses choose which Dynatrace MCP tools to call, which DQL to
    compose, and how to package what they find into a single Evidence.
    They never write prose; the synthesizer owns that.
    """

    #: Stable specialist name used as the ``Evidence.specialist`` value and
    #: as a Phoenix span tag. Must be unique across all registered specialists.
    name: str = ""

    #: Default hypothesis key emitted under partial-failure degradation.
    #: Subclasses pick a key that names "I didn't get to investigate"
    #: rather than guessing a real root cause.
    fallback_hypothesis_key: str = "unknown"

    def __init__(self, dynatrace: DynatraceClient) -> None:
        self._dynatrace = dynatrace

    @abstractmethod
    def investigate(self, signature: ProblemSignature) -> Evidence:
        """Return one Evidence summarizing this specialist's findings."""

    # Shared helper: subclasses call _safely() with their narrow probe
    # function. On Dynatrace partial failure, returns the standard
    # "informational, low confidence" Evidence so the synthesizer can
    # still render a brief without that specialist's input.
    def _safely(self, signature: ProblemSignature, probe) -> Evidence:
        try:
            return probe()
        except (DynatraceUnavailable, RateLimited) as exc:
            return Evidence(
                specialist=self.name,
                kind="log_pattern",
                summary=(
                    f"{self.name} unavailable: {exc.__class__.__name__}; "
                    "synthesizer should continue without this specialist."
                ),
                stance="informational",
                hypothesis_key=self.fallback_hypothesis_key,
                confidence=0.1,
            )
