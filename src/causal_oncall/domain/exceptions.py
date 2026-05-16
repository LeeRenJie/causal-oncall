"""Domain exceptions. Every cross-boundary error is one of these.

Hides: the underlying httpx / pymongo / MCP-stdio exception zoo. Callers
catch the domain exception that matches the behavior they care about,
not the concrete transport error.
"""

from __future__ import annotations


class CausalOnCallError(Exception):
    """Root of the domain exception hierarchy."""


class DynatraceUnavailable(CausalOnCallError):
    """The Dynatrace MCP server is unreachable or returned an unrecoverable error.

    Raised after the client has exhausted its internal retry budget. Callers
    should treat this as "the upstream observability stack is down" and
    degrade gracefully — not retry.
    """


class RateLimited(CausalOnCallError):
    """Dynatrace SaaS API returned 429 and the client's pacing budget is exhausted.

    Carries the suggested retry-after seconds when the server provides one.
    The orchestrator uses this to decide between waiting and short-circuiting
    to a partial brief.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class MemoryStoreUnavailable(CausalOnCallError):
    """Mongo Atlas is unreachable or the vector index is missing.

    The orchestrator must continue to a full investigation when this fires;
    the pre-flight memory match is a speed-up, not a hard dependency.
    """


class SynthesisFailed(CausalOnCallError):
    """The synthesizer's LLM call produced an unusable response.

    Wraps prompt-construction errors, model timeouts, and schema-validation
    failures on the model's structured output.
    """
