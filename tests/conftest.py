"""Shared pytest fixtures + reusable fakes.

The fakes here implement the same public surface as the real modules
(DynatraceClient, MemoryStore, Gemini calls). Tests inject them so no
unit test ever touches a real boundary; mocking what we own is banned
per ENGINEERING-PRINCIPLES §1.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from causal_oncall.domain.brief import Brief, Hypothesis
from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.exceptions import DynatraceUnavailable, RateLimited
from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import DQLPlan, Entity, ProblemContext, QueryResult

FIXTURES = Path(__file__).parent / "fixtures"


# ---------- factory helpers ---------- #


def make_signature(
    *,
    problem_id: str = "P-001",
    title: str = "Response time degradation on payment-service",
    severity: str = "PERFORMANCE",
    entity_ids: tuple[str, ...] = ("SERVICE-ABC123",),
    entity_types: tuple[str, ...] = ("SERVICE",),
    opened_at: datetime | None = None,
    fingerprint: str = "fp-001",
) -> ProblemSignature:
    return ProblemSignature(
        problem_id=problem_id,
        title=title,
        severity=severity,
        affected_entity_ids=entity_ids,
        affected_entity_types=entity_types,
        opened_at=opened_at or datetime(2026, 5, 17, 9, 30, tzinfo=UTC),
        fingerprint=fingerprint,
    )


def make_evidence(
    *,
    specialist: str = "triage",
    kind: str = "log_pattern",
    summary: str = "5xx burst on /charge starting 09:28:00",
    stance: str = "supports",
    hypothesis_key: str = "db_pool_exhaustion",
    confidence: float = 0.72,
    links: tuple[str, ...] = (),
) -> Evidence:
    return Evidence(
        specialist=specialist,
        kind=kind,  # type: ignore[arg-type]
        summary=summary,
        stance=stance,  # type: ignore[arg-type]
        hypothesis_key=hypothesis_key,
        confidence=confidence,
        dynatrace_links=links,
    )


def make_hypothesis(
    *,
    key: str = "db_pool_exhaustion",
    title: str = "DB connection pool exhausted by deploy v412",
    rank: int = 1,
    score: float = 0.81,
    supporting: tuple[Evidence, ...] = (),
    refuting: tuple[Evidence, ...] = (),
    next_action: str = "Roll back deploy v412 on payment-service.",
) -> Hypothesis:
    return Hypothesis(
        key=key,
        title=title,
        rank=rank,
        score=score,
        supporting_evidence=supporting,
        refuting_evidence=refuting,
        next_action=next_action,
    )


def make_brief(
    *,
    problem_id: str = "P-001",
    hypotheses: tuple[Hypothesis, ...] | None = None,
    memory_short_circuit: bool = False,
) -> Brief:
    hs = hypotheses or (make_hypothesis(),)
    return Brief(
        problem_id=problem_id,
        generated_at=datetime(2026, 5, 17, 9, 31, tzinfo=UTC),
        ranked_hypotheses=hs,
        top_recommendation=hs[0].next_action,
        memory_short_circuit=memory_short_circuit,
    )


# ---------- fakes ---------- #


class FakeDynatraceClient:
    """In-process stand-in for DynatraceClient.

    Tests pre-load tool responses keyed by tool name + a tag. Calls that
    aren't pre-loaded raise so tests fail loudly rather than silently.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._dql_results: dict[str, QueryResult] = {}
        self._topology: dict[str, list[Entity]] = {}
        self._problem_contexts: dict[str, ProblemContext] = {}
        self._comments: list[tuple[str, str]] = []
        self.fail_with: Exception | None = None
        self.rate_limit_after: int | None = None

    # configuration helpers
    def stub_problem_context(self, problem_id: str, ctx: ProblemContext) -> None:
        self._problem_contexts[problem_id] = ctx

    def stub_dql(self, tag: str, result: QueryResult) -> None:
        self._dql_results[tag] = result

    def stub_topology(self, entity_id: str, neighbors: list[Entity]) -> None:
        self._topology[entity_id] = neighbors

    # mimicked public surface
    def get_problem_context(self, problem_id: str) -> ProblemContext:
        self.calls.append(("get_problem_context", {"problem_id": problem_id}))
        self._maybe_fail()
        if problem_id not in self._problem_contexts:
            raise AssertionError(f"FakeDynatraceClient: no stubbed context for {problem_id!r}")
        return self._problem_contexts[problem_id]

    def execute_dql(self, plan: DQLPlan) -> QueryResult:
        self.calls.append(("execute_dql", {"query": plan.query, "params": plan.parameters}))
        self._maybe_fail()
        # tests tag stubs by an arbitrary substring match on the query
        for tag, result in self._dql_results.items():
            if tag in plan.query:
                return result
        raise AssertionError(f"FakeDynatraceClient: no stubbed DQL result matching {plan.query!r}")

    def get_topology_neighbors(self, entity_id: str, depth: int = 1) -> list[Entity]:
        self.calls.append(("get_topology_neighbors", {"entity_id": entity_id, "depth": depth}))
        self._maybe_fail()
        return list(self._topology.get(entity_id, []))

    def post_problem_comment(self, problem_id: str, markdown: str) -> str:
        self.calls.append(("post_problem_comment", {"problem_id": problem_id}))
        self._comments.append((problem_id, markdown))
        return f"comment-{len(self._comments)}"

    def close(self) -> None:
        self.calls.append(("close", {}))

    def _maybe_fail(self) -> None:
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc
        if self.rate_limit_after is not None and len(self.calls) > self.rate_limit_after:
            raise RateLimited("simulated 429", retry_after_seconds=1.0)


class FakeMemoryStore:
    """In-process stand-in for MemoryStore."""

    def __init__(self, *, match_to_return: Match | None = None) -> None:
        self.match_to_return = match_to_return
        self.recorded: list[IncidentRecord] = []
        self.resolutions: list[tuple[str, str, str]] = []
        self.fail_on_match: Exception | None = None

    def match(self, signature: ProblemSignature, *, threshold: float | None = None) -> Match | None:
        if self.fail_on_match is not None:
            raise self.fail_on_match
        if self.match_to_return is None:
            return None
        # Honor the threshold the caller passes if explicitly supplied.
        if threshold is not None and self.match_to_return.similarity < threshold:
            return None
        return self.match_to_return

    def record(self, incident_record: IncidentRecord) -> None:
        self.recorded.append(incident_record)

    def update_resolution(
        self, incident_id: str, *, confirmed_root_cause_key: str, confirmed_fix: str
    ) -> None:
        self.resolutions.append((incident_id, confirmed_root_cause_key, confirmed_fix))


class FakeSynthesizer:
    """In-process stand-in for Synthesizer."""

    def __init__(self, *, brief_to_return: Brief | None = None) -> None:
        self.brief_to_return = brief_to_return
        self.calls: list[tuple[ProblemSignature, tuple[Evidence, ...], bool]] = []

    def compose(
        self,
        signature: ProblemSignature,
        evidences,
        *,
        memory_short_circuit: bool = False,
    ) -> Brief:
        ev_tuple = tuple(evidences)
        self.calls.append((signature, ev_tuple, memory_short_circuit))
        if self.brief_to_return is None:
            return make_brief(
                problem_id=signature.problem_id,
                memory_short_circuit=memory_short_circuit,
            )
        return self.brief_to_return


class FakePhoenixTracer:
    """In-process stand-in for PhoenixTracer."""

    def __init__(self) -> None:
        self.spans: list[str] = []
        self.outcomes: list[tuple[str, bool]] = []

    def traced(self, agent_name: str):
        def deco(fn):
            def wrapper(*a, **kw):
                self.spans.append(agent_name)
                return fn(*a, **kw)

            return wrapper

        return deco

    def record_outcome(self, span_id: str, *, top_hypothesis_correct: bool) -> None:
        self.outcomes.append((span_id, top_hypothesis_correct))


# ---------- pytest fixtures ---------- #


@pytest.fixture
def fake_dynatrace() -> FakeDynatraceClient:
    return FakeDynatraceClient()


@pytest.fixture
def fake_memory() -> FakeMemoryStore:
    return FakeMemoryStore()


@pytest.fixture
def fake_synthesizer() -> FakeSynthesizer:
    return FakeSynthesizer()


@pytest.fixture
def fake_tracer() -> FakePhoenixTracer:
    return FakePhoenixTracer()


@pytest.fixture
def sample_signature() -> ProblemSignature:
    return make_signature()


@pytest.fixture
def incident_payloads() -> dict[str, dict[str, Any]]:
    """All five incident fixtures, keyed by filename stem."""
    out: dict[str, dict[str, Any]] = {}
    for path in (FIXTURES / "incidents").glob("*.json"):
        out[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return out


@pytest.fixture
def memory_seed_payload() -> list[dict[str, Any]]:
    return json.loads(
        (FIXTURES / "memory_seeds" / "seed_10_resolved.json").read_text(encoding="utf-8")
    )


# Re-export for convenience in test modules.
__all__ = [
    "DynatraceUnavailable",
    "FakeDynatraceClient",
    "FakeMemoryStore",
    "FakePhoenixTracer",
    "FakeSynthesizer",
    "RateLimited",
    "make_brief",
    "make_evidence",
    "make_hypothesis",
    "make_signature",
]
