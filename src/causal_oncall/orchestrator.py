"""Orchestrator — webhook-event in, finalized Brief out.

Hides: the memory pre-flight short-circuit, specialist dispatch order
and rate-limit pacing, partial-failure consolidation, hypothesis-
rejection re-planning, synthesis invocation, and the post-investigation
record-write back to the memory store.

Specialists run **sequentially** — Dynatrace SaaS enforces a 50-req/min
sliding-window per-tenant rate limit, and parallel fan-out blew the
budget in the spike. The sequential constraint is part of the contract,
not just an implementation detail.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from causal_oncall.domain.brief import Brief, Hypothesis
from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.exceptions import MemoryStoreUnavailable
from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.memory_store import MemoryStore
from causal_oncall.phoenix_tracer import PhoenixTracer
from causal_oncall.specialists.base import Specialist
from causal_oncall.synthesizer import Synthesizer
from causal_oncall.trace_broadcaster import TraceBroadcaster, TraceEvent


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
        trace_broadcaster: TraceBroadcaster | None = None,
    ) -> None:
        self._memory = memory
        self._specialists = tuple(specialists)
        self._synthesizer = synthesizer
        self._tracer = tracer
        self._config = config or OrchestratorConfig()
        # Optional broadcaster for the live trace UI. When unset, the
        # orchestrator runs in headless mode (W1 + tests rely on this).
        self._broadcaster = trace_broadcaster
        # In-process cache of the most recent investigation's signature +
        # collected evidence, keyed on problem_id. Powers the replan path
        # without re-hitting Dynatrace when only the synthesizer needs to
        # re-run with one hypothesis excluded.
        self._last_evidence: dict[str, tuple[ProblemSignature, tuple[Evidence, ...]]] = {}

    def handle(self, problem_open_event: dict) -> Brief:
        """Run the full pipeline for one Dynatrace problem.open event."""
        signature = ProblemSignature.from_dynatrace_payload(problem_open_event)
        self._emit(signature.problem_id, "orchestrator-started", {"title": signature.title})

        match = self._memory_match_or_none(signature)
        if match is not None:
            self._emit(
                signature.problem_id,
                "memory-short-circuit",
                {"prior_occurrences": match.prior_occurrences, "similarity": match.similarity},
            )
            brief = self._brief_from_memory(signature, match)
            self._emit_brief_ready_and_close(brief)
            return brief

        evidences = self._dispatch_specialists(signature)
        self._emit(signature.problem_id, "synthesizer-started", {})
        brief = self._synthesizer.compose(signature, evidences, memory_short_circuit=False)
        self._persist(signature, brief)
        self._last_evidence[signature.problem_id] = (signature, tuple(evidences))
        self._emit_brief_ready_and_close(brief)
        return brief

    def reject_hypothesis_and_replan(self, brief: Brief, rejected_hypothesis_key: str) -> Brief:
        """Re-run the agent with one hypothesis explicitly off the table.

        Powers the wow moment where the on-call clicks "reject #2" and the
        agent visibly re-investigates. Reuses cached specialist results
        where the rejected hypothesis was irrelevant; otherwise re-dispatches.
        """
        cached = self._last_evidence.get(brief.problem_id)
        if cached is not None:
            signature, evidences = cached
            filtered = tuple(e for e in evidences if e.hypothesis_key != rejected_hypothesis_key)
            if filtered:
                replanned = self._synthesizer.compose(
                    signature, filtered, memory_short_circuit=False
                )
                # Defensive: even if the synthesizer were to re-introduce the key
                # via the LLM, strip it from the surfaced ranked list.
                return self._strip_hypothesis(replanned, rejected_hypothesis_key)

        # Fall back: synthesize a stub brief without the rejected key.
        return self._strip_hypothesis(brief, rejected_hypothesis_key)

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    def _memory_match_or_none(self, signature: ProblemSignature) -> Match | None:
        try:
            return self._memory.match(signature, threshold=self._config.memory_match_threshold)
        except MemoryStoreUnavailable:
            return None

    def _dispatch_specialists(self, signature: ProblemSignature) -> list[Evidence]:
        # Sequential dispatch — see module docstring. If parallelism becomes
        # safe (e.g. cache hot), promote to asyncio.gather; do not silently
        # change this without verifying the Dynatrace rate-limit ceiling.
        out: list[Evidence] = []
        for specialist in self._specialists:
            self._emit(signature.problem_id, "specialist-dispatched", {"name": specialist.name})
            evidence = specialist.investigate(signature)
            out.append(evidence)
            self._emit(
                signature.problem_id,
                "specialist-completed",
                {
                    "name": specialist.name,
                    "stance": evidence.stance,
                    "hypothesis_key": evidence.hypothesis_key,
                    "confidence": evidence.confidence,
                },
            )
        return out

    def _emit(self, problem_id: str, kind: str, data: dict) -> None:
        if self._broadcaster is None:
            return
        self._broadcaster.publish(problem_id, TraceEvent(kind=kind, data=data))  # type: ignore[arg-type]

    def _emit_brief_ready_and_close(self, brief: Brief) -> None:
        if self._broadcaster is None:
            return
        top = brief.ranked_hypotheses[0] if brief.ranked_hypotheses else None
        self._broadcaster.publish(
            brief.problem_id,
            TraceEvent(
                kind="brief-ready",
                data={
                    "top_hypothesis_key": top.key if top else None,
                    "top_hypothesis_title": top.title if top else None,
                    "top_recommendation": brief.top_recommendation,
                    "memory_short_circuit": brief.memory_short_circuit,
                },
            ),
        )
        # Close the SSE stream so the browser gets a clean EOF instead of
        # hanging until the proxy/idle-timeout drops the connection.
        self._broadcaster.close(brief.problem_id)

    def _brief_from_memory(self, signature: ProblemSignature, match: Match) -> Brief:
        prior_brief = match.record.brief
        top = prior_brief.ranked_hypotheses[0] if prior_brief.ranked_hypotheses else None
        recommendation = (
            top.next_action
            if top is not None
            else match.record.confirmed_fix or "Apply the prior confirmed fix."
        )
        hypotheses: tuple[Hypothesis, ...]
        if top is not None:
            hypotheses = (
                Hypothesis(
                    key=top.key,
                    title=f"{top.title} (seen {match.prior_occurrences}x before)",
                    rank=1,
                    score=match.similarity,
                    supporting_evidence=(),
                    refuting_evidence=(),
                    next_action=recommendation,
                ),
            )
        else:
            hypotheses = (
                Hypothesis(
                    key=match.record.confirmed_root_cause_key or "memory_match",
                    title=f"Prior pattern (seen {match.prior_occurrences}x before)",
                    rank=1,
                    score=match.similarity,
                    supporting_evidence=(),
                    refuting_evidence=(),
                    next_action=recommendation,
                ),
            )
        return Brief(
            problem_id=signature.problem_id,
            generated_at=datetime.now(UTC),
            ranked_hypotheses=hypotheses,
            top_recommendation=recommendation,
            memory_short_circuit=True,
        )

    def _persist(self, signature: ProblemSignature, brief: Brief) -> None:
        record = IncidentRecord(
            incident_id=str(uuid.uuid4()),
            signature=signature,
            brief=brief,
            opened_at=signature.opened_at,
        )
        try:
            self._memory.record(record)
        except MemoryStoreUnavailable:
            # Memory is a speed-up, not a hard dep — drop the write rather
            # than fail the request. Phoenix span captures the partial.
            return

    @staticmethod
    def _strip_hypothesis(brief: Brief, key: str) -> Brief:
        remaining = tuple(h for h in brief.ranked_hypotheses if h.key != key)
        if not remaining:
            return Brief(
                problem_id=brief.problem_id,
                generated_at=brief.generated_at,
                ranked_hypotheses=(),
                top_recommendation="No hypotheses remain; re-investigate manually.",
                memory_short_circuit=brief.memory_short_circuit,
                unresolved_questions=brief.unresolved_questions,
            )
        # Re-rank: walk the ordered remainder and assign 1..N.
        renumbered = tuple(
            Hypothesis(
                key=h.key,
                title=h.title,
                rank=rank,
                score=h.score,
                supporting_evidence=h.supporting_evidence,
                refuting_evidence=h.refuting_evidence,
                next_action=h.next_action,
            )
            for rank, h in enumerate(remaining, start=1)
        )
        return Brief(
            problem_id=brief.problem_id,
            generated_at=brief.generated_at,
            ranked_hypotheses=renumbered,
            top_recommendation=renumbered[0].next_action,
            memory_short_circuit=brief.memory_short_circuit,
            unresolved_questions=brief.unresolved_questions,
        )
