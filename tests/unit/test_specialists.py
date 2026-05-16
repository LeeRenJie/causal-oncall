"""TDD spec for all five specialists — parameterized contract test.

Per ENGINEERING-PRINCIPLES §1 "Hackathon-specific TDD shortcut": the
specialist contract is uniform, so one parameterized suite covers every
implementation. Specialist-specific edge cases (e.g. Davis CoPilot
fallback for Triage) get their own targeted tests below.
"""

from __future__ import annotations

import pytest

from causal_oncall.domain.evidence import Evidence
from causal_oncall.dynatrace_client import Entity, ProblemContext, QueryResult
from causal_oncall.specialists import (
    AnomalyWindowSpecialist,
    DeployCorrelationSpecialist,
    Specialist,
    TopologySpecialist,
    TriageSpecialist,
    VulnSecSpecialist,
)
from tests.conftest import FakeDynatraceClient, make_signature


ALL_SPECIALIST_CLASSES = [
    TriageSpecialist,
    TopologySpecialist,
    DeployCorrelationSpecialist,
    AnomalyWindowSpecialist,
    VulnSecSpecialist,
]


def _seed_dynatrace(fd: FakeDynatraceClient) -> None:
    """Generic stubs broad enough that every specialist's normal path finds data."""
    sig = make_signature()
    fd.stub_problem_context(
        "P-001",
        ProblemContext(signature=sig, impacted_entities=({"id": "SERVICE-ABC"},), events_in_window=()),
    )
    fd.stub_dql(
        "fetch logs",
        QueryResult(columns=("ts", "level"), rows=((1, "ERROR"),), execution_ms=12),
    )
    fd.stub_dql(
        "fetch events",
        QueryResult(
            columns=("event_type", "ts"),
            rows=(("DEPLOY", 1), ("CHANGE", 2)),
            execution_ms=15,
        ),
    )
    fd.stub_dql(
        "fetch metric",
        QueryResult(
            columns=("metric", "deviation"),
            rows=(("service.responseTime", 4.2),),
            execution_ms=22,
        ),
    )
    fd.stub_dql(
        "fetch security.events",
        QueryResult(columns=("cve",), rows=(("CVE-2026-1234",),), execution_ms=10),
    )
    fd.stub_topology(
        "SERVICE-ABC",
        [Entity(entity_id="SERVICE-DEF", entity_type="SERVICE", display_name="db", distance=1)],
    )


@pytest.mark.parametrize("cls", ALL_SPECIALIST_CLASSES, ids=lambda c: c.name)
def test_specialist_returns_evidence_with_matching_specialist_name(cls):
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    s: Specialist = cls(fd)  # type: ignore[arg-type]
    ev = s.investigate(make_signature(problem_id="P-001"))
    assert isinstance(ev, Evidence)
    assert ev.specialist == s.name


@pytest.mark.parametrize("cls", ALL_SPECIALIST_CLASSES, ids=lambda c: c.name)
def test_specialist_evidence_confidence_is_in_unit_interval(cls):
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    s: Specialist = cls(fd)  # type: ignore[arg-type]
    ev = s.investigate(make_signature(problem_id="P-001"))
    assert 0.0 <= ev.confidence <= 1.0


@pytest.mark.parametrize("cls", ALL_SPECIALIST_CLASSES, ids=lambda c: c.name)
def test_specialist_falls_back_to_informational_on_dynatrace_partial_failure(cls):
    """Spec §1: specialists never raise on partial Dynatrace failure."""
    from causal_oncall.domain.exceptions import DynatraceUnavailable

    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    # First call succeeds (problem context); subsequent fail.
    fd.fail_with = DynatraceUnavailable("Grail flaky")

    s: Specialist = cls(fd)  # type: ignore[arg-type]
    ev = s.investigate(make_signature(problem_id="P-001"))
    # The specialist should degrade rather than bubble — confidence drops,
    # stance shifts to informational.
    assert ev.stance == "informational"
    assert ev.confidence <= 0.4


@pytest.mark.parametrize("cls", ALL_SPECIALIST_CLASSES, ids=lambda c: c.name)
def test_specialist_only_calls_allowed_dynatrace_methods(cls):
    """Specialists must never bypass DynatraceClient (e.g. raw HTTP)."""
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    s: Specialist = cls(fd)  # type: ignore[arg-type]
    s.investigate(make_signature(problem_id="P-001"))

    allowed = {"get_problem_context", "execute_dql", "get_topology_neighbors"}
    used = {name for name, _ in fd.calls}
    assert used.issubset(allowed), f"Specialist used disallowed methods: {used - allowed}"


# ----- specialist-specific behaviors -----


def test_deploy_correlation_assigns_higher_confidence_when_deploy_inside_window():
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    s = DeployCorrelationSpecialist(fd)
    ev = s.investigate(make_signature(problem_id="P-001"))
    # The seeded events stub above includes a DEPLOY event — confidence
    # should reflect that, not the floor.
    assert ev.confidence > 0.4
