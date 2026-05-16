"""Orchestrator — webhook-event in, finalized Brief out.

Hides: the memory pre-flight short-circuit, specialist dispatch order
and rate-limit pacing, partial-failure consolidation, hypothesis-
rejection re-planning, synthesis invocation, and the post-investigation
record-write back to the memory store.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from causal_oncall.domain.brief import Brief
from causal_oncall.memory_store import MemoryStore
from causal_oncall.phoenix_tracer import PhoenixTracer
from causal_oncall.specialists.base import Specialist
from causal_oncall.synthesizer import Synthesizer


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    """All knobs that affect orchestration policy."""

    memory_match_threshold: float = 0.85
    specialist_dispatch_budget_seconds: float = 60.0


class Orchestrator:
    """Top-level coordinator.

    A single call to :meth:`handle` runs the full agent flow:
      1. Normalize the inbound webhook event to a ProblemSignature.
      2. Query the memory store; if a high-confidence match exists,
         short-circuit to a memory-only brief.
      3. Otherwise dispatch the specialists (sequentially, paced for
         the Dynatrace rate limit) and collect their Evidence.
      4. Hand the bag of Evidence to the Synthesizer.
      5. Persist the resulting Brief + signature to the memory store
         so future incidents can short-circuit on this one.
    """

    def __init__(
        self,
        *,
        memory: MemoryStore,
        specialists: Sequence[Specialist],
        synthesizer: Synthesizer,
        tracer: PhoenixTracer,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self._memory = memory
        self._specialists = tuple(specialists)
        self._synthesizer = synthesizer
        self._tracer = tracer
        self._config = config or OrchestratorConfig()

    def handle(self, problem_open_event: dict) -> Brief:
        """Run the full pipeline for one Dynatrace problem.open event.

        Idempotent on ``problem_id``: re-running the same event returns
        an equivalent brief (modulo timestamps) and does not double-write
        to the memory store.

        The returned Brief is what gets posted to Slack and Dynatrace;
        callers do not post it themselves — they get it back and decide
        what to do with it (the FastAPI handler posts; tests inspect).
        """
        raise NotImplementedError(
            "Normalize event → ProblemSignature, run memory pre-flight, dispatch "
            "specialists or short-circuit, synthesize, persist, return Brief."
        )

    def reject_hypothesis_and_replan(
        self, brief: Brief, rejected_hypothesis_key: str
    ) -> Brief:
        """Re-run the agent with one hypothesis explicitly off the table.

        Powers the wow moment where the on-call clicks "reject #2" and the
        agent visibly re-investigates. Reuses cached specialist results
        where the rejected hypothesis was irrelevant; otherwise re-dispatches.
        """
        raise NotImplementedError(
            "Replan investigation with rejected_hypothesis_key excluded and "
            "return a freshly synthesized Brief."
        )
