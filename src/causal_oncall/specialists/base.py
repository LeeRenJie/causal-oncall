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

    #: Subset of ``DynatraceClient`` public methods this specialist is
    #: permitted to call. Used as documentation + as the contract-test
    #: oracle for the "specialist-uses-narrow-toolset" invariant. Empty
    #: tuple = unrestricted (default for the abstract base only).
    allowed_dynatrace_methods: tuple[str, ...] = ()

    def __init__(self, dynatrace: DynatraceClient) -> None:
        self._dynatrace = dynatrace

    @abstractmethod
    def investigate(
        self,
        signature: ProblemSignature,
        *,
        prior_hypothesis: str | None = None,
    ) -> Evidence:
        """Return one Evidence summarizing this specialist's findings.

        The optional ``prior_hypothesis`` kwarg is set by the orchestrator
        when a medium-confidence memory match exists (W3-S2 3-tier
        short-circuit). Specialists are free to use it as a bias signal
        for their investigation — e.g. by tightening their DQL filter to
        the known hypothesis's expected entity types — or ignore it
        entirely; the contract guarantees that the synthesizer still
        receives a fully-formed Evidence either way. Today every shipped
        specialist accepts the kwarg but only logs it via the
        partial-failure path; the W3 postmortem will revisit whether
        per-specialist bias logic earns its complexity.
        """

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
