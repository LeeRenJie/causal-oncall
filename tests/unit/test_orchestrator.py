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

    def investigate(self, signature):
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
        specialists=specialists or [_StubSpecialist("triage", make_evidence(specialist="triage"))],
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
    memory = FakeMemoryStore(
        match_to_return=Match(record=rec, similarity=0.95, prior_occurrences=14)
    )
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
    triage = _StubSpecialist("triage", make_evidence(specialist="triage", hypothesis_key="A"))
    topo = _StubSpecialist("topology", make_evidence(specialist="topology", hypothesis_key="B"))
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


def test_replan_synthesizes_a_fresh_brief_when_cache_has_remaining_keys():
    """When >1 hypothesis_key was investigated, replan re-synthesizes minus the rejected one."""
    h_remaining = make_hypothesis(key="keep", title="Keep me", rank=2, score=0.5)
    h_other = make_hypothesis(key="keep_other", title="Keep me too", rank=3, score=0.4)
    synth = FakeSynthesizer(
        brief_to_return=make_brief(
            problem_id="P-1",  # must match _PROBLEM_EVENT.problemId for the cache to hit
            hypotheses=(make_hypothesis(key="db_pool_exhaustion"), h_remaining, h_other),
        )
    )
    triage = _StubSpecialist(
        "triage", make_evidence(hypothesis_key="db_pool_exhaustion", stance="supports")
    )
    topo = _StubSpecialist(
        "topology",
        make_evidence(specialist="topology", hypothesis_key="keep", stance="supports"),
    )
    orch = _make_orchestrator(specialists=[triage, topo], synthesizer=synth)
    first = orch.handle(_PROBLEM_EVENT)

    # Set synthesizer to return a brief that does include 'keep' but not 'db_pool_exhaustion'.
    synth.brief_to_return = make_brief(problem_id="P-1", hypotheses=(h_remaining, h_other))
    replanned = orch.reject_hypothesis_and_replan(first, "db_pool_exhaustion")

    # Re-ranked starting at 1.
    ranks = [h.rank for h in replanned.ranked_hypotheses]
    assert ranks == [1, 2]
    assert {h.key for h in replanned.ranked_hypotheses} == {"keep", "keep_other"}


def test_orchestrator_uses_memory_record_when_prior_brief_has_no_hypotheses(monkeypatch):
    """If the prior IncidentRecord's brief lost its hypothesis tree, fall back to the confirmed fix."""
    sig = make_signature()
    # Build a record whose stored brief has empty ranked_hypotheses.
    empty_brief = make_brief(hypotheses=(make_hypothesis(),))
    from causal_oncall.domain.brief import Brief

    bare_brief = Brief(
        problem_id=empty_brief.problem_id,
        generated_at=empty_brief.generated_at,
        ranked_hypotheses=(),
        top_recommendation="legacy fix",
    )
    rec = IncidentRecord(
        incident_id="prior-1",
        signature=sig,
        brief=bare_brief,
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased pool size",
    )
    memory = FakeMemoryStore(
        match_to_return=Match(record=rec, similarity=0.99, prior_occurrences=7)
    )
    orch = _make_orchestrator(memory=memory)
    brief = orch.handle(_PROBLEM_EVENT)
    assert brief.memory_short_circuit is True
    assert brief.ranked_hypotheses[0].key == "db_pool_exhaustion"


def test_orchestrator_swallows_memory_store_unavailable_during_record():
    """If persisting fails, the agent still returns the brief — memory is best-effort."""
    from causal_oncall.domain.exceptions import MemoryStoreUnavailable

    class _FlakyMemory(FakeMemoryStore):
        def record(self, incident_record):
            raise MemoryStoreUnavailable("atlas write blocked")

    orch = _make_orchestrator(memory=_FlakyMemory())
    brief = orch.handle(_PROBLEM_EVENT)
    assert brief is not None


def test_replan_returns_empty_recommendation_when_rejection_removes_all_hypotheses():
    """All hypotheses share the rejected key → surface a clear no-op brief."""
    triage = _StubSpecialist(
        "triage", make_evidence(hypothesis_key="db_pool_exhaustion", stance="supports")
    )
    orch = _make_orchestrator(specialists=[triage])
    first = orch.handle(_PROBLEM_EVENT)
    replanned = orch.reject_hypothesis_and_replan(first, "db_pool_exhaustion")
    assert replanned.ranked_hypotheses == ()
    assert "re-investigate" in replanned.top_recommendation.lower()


def test_replan_with_unknown_brief_id_falls_back_to_strip_only():
    """Replan path with no cached evidence still returns a brief minus the key."""
    orch = _make_orchestrator()
    h_a = make_hypothesis(key="a", rank=1)
    h_b = make_hypothesis(key="b", rank=2)
    stranger_brief = make_brief(problem_id="P-UNKNOWN", hypotheses=(h_a, h_b))
    replanned = orch.reject_hypothesis_and_replan(stranger_brief, "a")
    assert {h.key for h in replanned.ranked_hypotheses} == {"b"}
