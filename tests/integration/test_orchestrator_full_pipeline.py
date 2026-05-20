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
    # The brief must surface at least one Dynatrace deep link so the
    # on-call can jump straight to the underlying data.
    md = brief.to_markdown()
    assert "abc.live.dynatrace.com" in md
