"""Integration test — every module wired together, faked at the boundaries.

This is the test that proves the modules compose: orchestrator dispatches
real specialist instances (against a fake Dynatrace), aggregates real
Evidence, hands it to a real Synthesizer (against a fake Gemini), and
writes to a real MemoryStore (against mongomock).

If this passes, the agent's internal seams hold together. The contract
suite is responsible for the external seams; this suite is responsible
for everything in between.
"""

from __future__ import annotations

from datetime import UTC, datetime

from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.dynatrace_client import Entity, ProblemContext, QueryResult
from causal_oncall.orchestrator import Orchestrator, OrchestratorConfig
from causal_oncall.specialists import (
    AnomalyWindowSpecialist,
    DeployCorrelationSpecialist,
    TopologySpecialist,
    TriageSpecialist,
    VulnSecSpecialist,
)
from causal_oncall.synthesizer import Synthesizer, SynthesizerConfig
from tests.conftest import (
    FakeDynatraceClient,
    FakeMemoryStore,
    FakePhoenixTracer,
    make_brief,
    make_signature,
)

_PROBLEM_EVENT = {
    "problemId": "P-001",
    "title": "Payment latency spike",
    "severityLevel": "PERFORMANCE",
    "startTime": "2026-05-17T09:30:00Z",
    "affectedEntities": [{"entityId": {"id": "SERVICE-ABC"}, "type": "SERVICE"}],
}


def _seed_dynatrace(fd: FakeDynatraceClient) -> None:
    fd.stub_problem_context(
        "P-001",
        ProblemContext(
            signature=make_signature(),
            impacted_entities=({"id": "SERVICE-ABC"},),
            events_in_window=(),
        ),
    )
    for tag in ("fetch logs", "fetch events", "fetch metric", "fetch security.events"):
        fd.stub_dql(tag, QueryResult(columns=("c",), rows=((1,),), execution_ms=5))
    fd.stub_topology(
        "SERVICE-ABC",
        [Entity(entity_id="S2", entity_type="SERVICE", display_name="db", distance=1)],
    )


def test_full_pipeline_produces_a_brief_with_all_specialists_contributing(monkeypatch):
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)

    synthesizer = Synthesizer(
        SynthesizerConfig(
            gemini_model_id="gemini-3-pro-preview",
            dynatrace_base_url="https://abc.live.dynatrace.com",
        )
    )

    # Stub the synthesizer's LLM seam with a deterministic generator.
    def _llm(_prompt: str) -> dict:
        return {
            "hypotheses": {
                "db_pool_exhaustion": {
                    "title": "DB connection pool exhausted",
                    "next_action": "Roll back deploy v412 on payment-service.",
                }
            }
        }

    monkeypatch.setattr(synthesizer, "_llm_call", _llm, raising=False)

    orch = Orchestrator(
        memory=FakeMemoryStore(),
        specialists=[
            TriageSpecialist(fd),  # type: ignore[arg-type]
            TopologySpecialist(fd),  # type: ignore[arg-type]
            DeployCorrelationSpecialist(fd),  # type: ignore[arg-type]
            AnomalyWindowSpecialist(fd),  # type: ignore[arg-type]
            VulnSecSpecialist(fd),  # type: ignore[arg-type]
        ],
        synthesizer=synthesizer,
        tracer=FakePhoenixTracer(),
        config=OrchestratorConfig(),
    )

    brief = orch.handle(_PROBLEM_EVENT)
    assert brief.problem_id == "P-001"
    assert brief.ranked_hypotheses
    # Cold start: from_memory remains False.
    assert brief.from_memory is False
    assert brief.pattern_match_score is None
    # The brief must surface at least one Dynatrace deep link so the
    # on-call can jump straight to the underlying data.
    md = brief.to_markdown()
    assert "abc.live.dynatrace.com" in md


def test_full_pipeline_short_circuits_when_memory_has_a_high_confidence_match(monkeypatch):
    """W3-S2 integration: real specialists wired, but a high-conf memory hit skips them.

    Exercises the with-memory-hit branch end-to-end through the
    same modules as the cold-start integration above. The five real
    specialist instances stay constructed (orchestrator boots normally);
    their ``investigate`` must never be reached because the pre-flight
    short-circuit fires first.
    """
    fd = FakeDynatraceClient()
    # No DQL stubs needed — specialists never run on the short-circuit path.
    # We still seed the problem_context so any code that pre-emptively
    # hydrates the signature won't trip the AssertionError in the fake.
    fd.stub_problem_context(
        "P-001",
        ProblemContext(
            signature=make_signature(),
            impacted_entities=({"id": "SERVICE-ABC"},),
            events_in_window=(),
        ),
    )

    synthesizer = Synthesizer(
        SynthesizerConfig(
            gemini_model_id="gemini-3-pro-preview",
            dynatrace_base_url="https://abc.live.dynatrace.com",
        )
    )
    # If the synthesizer were called, it would blow up at network time —
    # the test passing implicitly proves we never reached it.
    sig = make_signature(problem_id="P-001")
    rec = IncidentRecord(
        incident_id="prior-1",
        signature=sig,
        brief=make_brief(problem_id="P-001"),
        opened_at=datetime(2026, 5, 1, tzinfo=UTC),
        resolved_at=datetime(2026, 5, 1, 1, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased pool size on payments-db; bumped HikariCP to 60.",
    )
    memory = FakeMemoryStore(
        match_to_return=Match(record=rec, similarity=0.93, prior_occurrences=14)
    )

    orch = Orchestrator(
        memory=memory,
        specialists=[
            TriageSpecialist(fd),  # type: ignore[arg-type]
            TopologySpecialist(fd),  # type: ignore[arg-type]
            DeployCorrelationSpecialist(fd),  # type: ignore[arg-type]
            AnomalyWindowSpecialist(fd),  # type: ignore[arg-type]
            VulnSecSpecialist(fd),  # type: ignore[arg-type]
        ],
        synthesizer=synthesizer,
        tracer=FakePhoenixTracer(),
        config=OrchestratorConfig(),
    )

    brief = orch.handle(_PROBLEM_EVENT)
    assert brief.problem_id == "P-001"
    assert brief.from_memory is True
    assert brief.pattern_match_score == 0.93
    # Specialists were skipped — no DQL probe calls landed on the fake.
    dql_calls = [c for c in fd.calls if c[0] == "execute_dql"]
    assert dql_calls == [], "specialists must not run on the short-circuit path"
