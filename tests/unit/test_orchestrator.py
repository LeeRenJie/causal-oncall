"""TDD spec for Orchestrator.

The orchestrator is the agent's executive function. Its decisions —
pre-flight memory short-circuit, sequential specialist dispatch,
hypothesis-rejection replan — are the most behaviorally-rich code in
the codebase and the most worth testing.
"""

from __future__ import annotations

from datetime import UTC, datetime

from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.orchestrator import Orchestrator, OrchestratorConfig
from causal_oncall.specialists.base import Specialist
from tests.conftest import (
    FakeMemoryStore,
    FakePhoenixTracer,
    FakeSynthesizer,
    make_brief,
    make_evidence,
    make_hypothesis,
    make_signature,
)


class _StubSpecialist(Specialist):
    """Test specialist whose investigate() returns a pre-canned Evidence."""

    name = "stub"

    def __init__(self, name: str, evidence: Evidence) -> None:
        super().__init__(dynatrace=None)  # type: ignore[arg-type]
        self.name = name
        self._evidence = evidence
        self.calls = 0

    def investigate(self, signature):  # noqa: D401
        self.calls += 1
        return self._evidence


_PROBLEM_EVENT = {
    "problemId": "P-1",
    "title": "latency",
    "severityLevel": "PERFORMANCE",
    "startTime": "2026-05-17T09:30:00Z",
    "affectedEntities": [{"entityId": {"id": "SERVICE-ABC"}, "type": "SERVICE"}],
}


def _make_orchestrator(
    *, memory=None, synthesizer=None, specialists=None, config=None
) -> Orchestrator:
    return Orchestrator(
        memory=memory or FakeMemoryStore(),
        specialists=specialists
        or [_StubSpecialist("triage", make_evidence(specialist="triage"))],
        synthesizer=synthesizer or FakeSynthesizer(),
        tracer=FakePhoenixTracer(),
        config=config or OrchestratorConfig(),
    )


def test_orchestrator_skips_specialists_when_memory_match_is_high_confidence():
    """Wow-moment #3: a high-confidence match short-circuits investigation."""
    sig = make_signature()
    rec = IncidentRecord(
        incident_id="prior-1",
        signature=sig,
        brief=make_brief(),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased pool size",
    )
    memory = FakeMemoryStore(match_to_return=Match(record=rec, similarity=0.95, prior_occurrences=14))
    triage = _StubSpecialist("triage", make_evidence())

    orch = _make_orchestrator(
        memory=memory,
        specialists=[triage],
        config=OrchestratorConfig(memory_match_threshold=0.85),
    )
    brief = orch.handle(_PROBLEM_EVENT)
    assert triage.calls == 0
    assert brief.memory_short_circuit is True


def test_orchestrator_dispatches_all_specialists_when_no_memory_match():
    triage = _StubSpecialist("triage", make_evidence(specialist="triage"))
    topo = _StubSpecialist("topology", make_evidence(specialist="topology"))
    orch = _make_orchestrator(specialists=[triage, topo])
    orch.handle(_PROBLEM_EVENT)
    assert triage.calls == 1
    assert topo.calls == 1


def test_orchestrator_passes_aggregated_evidence_to_synthesizer():
    triage = _StubSpecialist(
        "triage", make_evidence(specialist="triage", hypothesis_key="A")
    )
    topo = _StubSpecialist(
        "topology", make_evidence(specialist="topology", hypothesis_key="B")
    )
    synth = FakeSynthesizer()
    orch = _make_orchestrator(specialists=[triage, topo], synthesizer=synth)
    orch.handle(_PROBLEM_EVENT)

    assert len(synth.calls) == 1
    _, evidences, short_circuit = synth.calls[0]
    assert short_circuit is False
    specialists_seen = {e.specialist for e in evidences}
    assert specialists_seen == {"triage", "topology"}


def test_orchestrator_records_brief_to_memory_after_synthesis():
    memory = FakeMemoryStore()
    orch = _make_orchestrator(memory=memory)
    orch.handle(_PROBLEM_EVENT)
    assert len(memory.recorded) == 1


def test_orchestrator_continues_when_memory_store_is_unavailable():
    """Memory is a speed-up, not a hard dep. The agent must still produce a brief."""
    from causal_oncall.domain.exceptions import MemoryStoreUnavailable

    memory = FakeMemoryStore()
    memory.fail_on_match = MemoryStoreUnavailable("atlas down")
    triage = _StubSpecialist("triage", make_evidence())
    orch = _make_orchestrator(memory=memory, specialists=[triage])

    brief = orch.handle(_PROBLEM_EVENT)
    assert brief is not None
    assert triage.calls == 1  # fell through to full investigation


def test_replan_with_rejected_hypothesis_excludes_it_from_new_brief():
    """Wow-moment #2: live replan after on-call rejects hypothesis #2."""
    triage = _StubSpecialist(
        "triage",
        make_evidence(hypothesis_key="db_pool_exhaustion", stance="supports"),
    )
    orch = _make_orchestrator(specialists=[triage])
    first = orch.handle(_PROBLEM_EVENT)

    replanned = orch.reject_hypothesis_and_replan(first, "db_pool_exhaustion")
    keys = {h.key for h in replanned.ranked_hypotheses}
    assert "db_pool_exhaustion" not in keys
