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
        ProblemContext(
            signature=sig, impacted_entities=({"id": "SERVICE-ABC"},), events_in_window=()
        ),
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
    # W2-S1: the degraded Evidence carries the specialist's fallback key
    # so the synthesizer can still tell "I didn't get to investigate" apart
    # from a real hypothesis judgment.
    assert ev.hypothesis_key == s.fallback_hypothesis_key
    assert ev.summary, "fallback Evidence must explain why the specialist degraded"


@pytest.mark.parametrize("cls", ALL_SPECIALIST_CLASSES, ids=lambda c: c.name)
def test_specialist_only_calls_allowed_dynatrace_methods(cls):
    """Specialists must never bypass DynatraceClient (e.g. raw HTTP) and must
    stay inside their declared narrow toolset (``allowed_dynatrace_methods``).
    """
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    s: Specialist = cls(fd)  # type: ignore[arg-type]
    s.investigate(make_signature(problem_id="P-001"))

    declared = set(s.allowed_dynatrace_methods)
    assert declared, f"{cls.__name__} must declare a non-empty allowed_dynatrace_methods"
    used = {name for name, _ in fd.calls}
    assert used.issubset(
        declared
    ), f"{cls.__name__} called {used - declared!r}; declared {declared!r}"


@pytest.mark.parametrize("cls", ALL_SPECIALIST_CLASSES, ids=lambda c: c.name)
def test_specialist_evidence_has_non_empty_summary(cls):
    """Every Evidence must carry text the synthesizer can render."""
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    s: Specialist = cls(fd)  # type: ignore[arg-type]
    ev = s.investigate(make_signature(problem_id="P-001"))
    assert ev.summary
    assert ev.hypothesis_key  # non-empty even on the happy path


# ----- specialist-specific behaviors -----


def test_deploy_correlation_assigns_higher_confidence_when_deploy_inside_window():
    fd = FakeDynatraceClient()
    _seed_dynatrace(fd)
    s = DeployCorrelationSpecialist(fd)
    ev = s.investigate(make_signature(problem_id="P-001"))
    # The seeded events stub above includes a DEPLOY event — confidence
    # should reflect that, not the floor.
    assert ev.confidence > 0.4


def test_deploy_correlation_refutes_when_no_deploy_in_window():
    """The 'no DEPLOY rows' branch lowers confidence and flips stance to refutes."""
    fd = FakeDynatraceClient()
    fd.stub_problem_context(
        "P-001",
        ProblemContext(
            signature=make_signature(),
            impacted_entities=({"id": "SERVICE-ABC"},),
            events_in_window=(),
        ),
    )
    # Events DQL returns rows but no DEPLOY cell.
    fd.stub_dql(
        "fetch events",
        QueryResult(
            columns=("event_type",),
            rows=(("CHANGE",), ("WARNING",)),
            execution_ms=5,
        ),
    )
    s = DeployCorrelationSpecialist(fd)
    ev = s.investigate(make_signature(problem_id="P-001"))
    assert ev.stance == "refutes"
    assert ev.confidence <= 0.5


def test_anomaly_window_refutes_when_no_deviations_detected():
    fd = FakeDynatraceClient()
    fd.stub_problem_context(
        "P-001",
        ProblemContext(
            signature=make_signature(),
            impacted_entities=({"id": "SERVICE-ABC"},),
            events_in_window=(),
        ),
    )
    fd.stub_dql(
        "fetch metric",
        QueryResult(columns=("metric",), rows=(), execution_ms=5),
    )
    s = AnomalyWindowSpecialist(fd)
    ev = s.investigate(make_signature(problem_id="P-001"))
    assert ev.stance == "refutes"
    assert ev.confidence < 0.5


def test_vuln_sec_refutes_when_no_cves_overlap_the_window():
    fd = FakeDynatraceClient()
    fd.stub_problem_context(
        "P-001",
        ProblemContext(
            signature=make_signature(),
            impacted_entities=({"id": "SERVICE-ABC"},),
            events_in_window=(),
        ),
    )
    fd.stub_dql(
        "fetch security.events",
        QueryResult(columns=("cve",), rows=(), execution_ms=5),
    )
    s = VulnSecSpecialist(fd)
    ev = s.investigate(make_signature(problem_id="P-001"))
    assert ev.stance == "refutes"
    assert ev.confidence < 0.5


def test_topology_emits_informational_when_no_neighbors_found():
    """Empty topology degrades to informational (not supports)."""
    fd = FakeDynatraceClient()
    fd.stub_problem_context(
        "P-001",
        ProblemContext(
            signature=make_signature(),
            impacted_entities=({"id": "SERVICE-ABC"},),
            events_in_window=(),
        ),
    )
    # No stub_topology -> default empty list.
    s = TopologySpecialist(fd)
    ev = s.investigate(make_signature(problem_id="P-001"))
    assert ev.stance == "informational"
