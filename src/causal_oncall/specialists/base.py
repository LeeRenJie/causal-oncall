"""Specialist ABC — the one contract every sub-agent honors.

Hides: nothing in itself. Defines the seam by which the orchestrator
can dispatch specialists without knowing their internals, and by which
new specialists can be added without changing the orchestrator.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from causal_oncall.domain.evidence import Evidence
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

    def __init__(self, dynatrace: DynatraceClient) -> None:
        self._dynatrace = dynatrace

    @abstractmethod
    def investigate(self, signature: ProblemSignature) -> Evidence:
        """Return one Evidence summarizing this specialist's findings.

        Implementations must:
          * call only Dynatrace MCP tools (no direct HTTP),
          * return Evidence whose ``specialist`` field matches ``self.name``,
          * never raise on Dynatrace partial failures — fall back to
            ``stance="informational"`` with a low confidence instead so
            the synthesizer can still produce a brief.
        """
        raise NotImplementedError(
            "Specialist subclasses must implement investigate() and return an Evidence "
            "whose specialist field matches self.name."
        )
