"""Demo wiring — in-process fakes for the Cloud Run live demo path.

This module is glue. Its only purpose is to let the Cloud Run service
boot and serve the full demo path (webhook -> orchestrator -> brief +
trace SSE + dashboard) WITHOUT requiring live Dynatrace OAuth, Mongo
Atlas, Gemini, or Slack credentials.

Why this module lives under ``src/`` rather than re-using ``tests/``:
the Docker image deliberately does not COPY ``tests/`` (per
``.dockerignore``) — production code must not depend on test scaffolding.
This module duplicates the minimum fake surface the dev/demo wiring
needs, isolated from the test-suite's pytest fixtures.

Activation:
  * ``CAUSAL_ONCALL_DEV_MODE=1`` (legacy local-dev gate) — kept for the
    W1-S3 curl smoke command in BUILD-LOG.md.
  * ``CAUSAL_ONCALL_DEMO_MODE=true`` (W4-S1 Cloud Run gate) — judges'
    demo URL boots here so ``?demo=true`` dashboards + the webhook curl
    both work without standing up the partner-bucket integrations live.

Production wiring (real Dynatrace MCP + real Mongo + real Gemini + real
Slack) is in ``app.py::_build_production_wiring`` and stays unchanged.
The OAuth-client hard-blocker for Cloud Run live Dynatrace MCP calls is
documented in BUILD-LOG.md W4-S1.
"""

# pragma: no cover (whole module — only loaded by app.py glue path)

from __future__ import annotations

from causal_oncall.domain.brief import Brief, Hypothesis
from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.incident_record import IncidentRecord, Match
from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import (
    DQLPlan,
    Entity,
    EventId,
    ProblemContext,
    QueryResult,
)


def make_signature() -> ProblemSignature:  # pragma: no cover
    from datetime import UTC, datetime

    return ProblemSignature(
        problem_id="P-001",
        title="Response time degradation on payment-service",
        severity="PERFORMANCE",
        affected_entity_ids=("SERVICE-ABC123",),
        affected_entity_types=("SERVICE",),
        opened_at=datetime(2026, 5, 17, 9, 30, tzinfo=UTC),
        fingerprint="fp-001",
    )


class _DemoDynatraceClient:  # pragma: no cover
    """In-process stand-in for DynatraceClient — demo-only.

    Mirrors the same public surface FakeDynatraceClient exposes in the
    test suite, narrowed to what the orchestrator dispatch path needs.
    Every DQL probe returns a canned QueryResult that the specialists'
    hypothesis-key emission keys off.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._dql_results: dict[str, QueryResult] = {}
        self._topology: dict[str, list[Entity]] = {}
        self._problem_contexts: dict[str, ProblemContext] = {}
        self._events: list[tuple[str, str, str]] = []

    def stub_problem_context(self, problem_id: str, ctx: ProblemContext) -> None:
        self._problem_contexts[problem_id] = ctx

    def stub_dql(self, tag: str, result: QueryResult) -> None:
        self._dql_results[tag] = result

    def stub_topology(self, entity_id: str, neighbors: list[Entity]) -> None:
        self._topology[entity_id] = neighbors

    def get_problem_context(self, problem_id: str) -> ProblemContext:
        self.calls.append(("get_problem_context", {"problem_id": problem_id}))
        if problem_id in self._problem_contexts:
            return self._problem_contexts[problem_id]
        # Fall back to the canonical demo signature for unknown ids so
        # the live demo never 500s on an unfamiliar payload.
        sig = make_signature()
        return ProblemContext(
            signature=sig,
            impacted_entities=({"id": "SERVICE-ABC123"},),
            events_in_window=(),
        )

    def execute_dql(self, plan: DQLPlan) -> QueryResult:
        self.calls.append(("execute_dql", {"query": plan.query, "params": plan.parameters}))
        for tag, result in self._dql_results.items():
            if tag in plan.query:
                return result
        return QueryResult(columns=(), rows=(), execution_ms=0)

    def get_topology_neighbors(self, entity_id: str, depth: int = 1) -> list[Entity]:
        self.calls.append(("get_topology_neighbors", {"entity_id": entity_id, "depth": depth}))
        return list(self._topology.get(entity_id, []))

    def send_investigation_event(
        self, problem_id: str, brief_md: str, hypothesis_summary: str
    ) -> EventId:
        self.calls.append(("send_investigation_event", {"problem_id": problem_id}))
        self._events.append((problem_id, brief_md, hypothesis_summary))
        return EventId(
            investigation_id=f"causal-oncall-{problem_id}-demo{len(self._events):04x}",
            upstream_reference=f"event-{len(self._events)}",
        )

    def close(self) -> None:
        self.calls.append(("close", {}))


class _DemoMemoryStore:  # pragma: no cover
    """In-process stand-in for MemoryStore — demo-only.

    Returns a canned Match for the db_pool_exhaustion fixture so wow #3
    (memory short-circuit + "seen this 14x" badge) fires on a live curl
    against the demo URL. All other problem_ids fall through and run the
    full specialist dispatch (wow #1 + #2).
    """

    # Problem id from tests/fixtures/incidents/db_pool_exhaustion.json — the
    # second canonical demo fixture. First curl hits payment_latency_spike.json
    # (cold path, wow #1+#2); second curl hits db_pool_exhaustion.json
    # (memory hit, wow #3).
    _SHORT_CIRCUIT_PROBLEM_ID = "-9223372036854775806_v2"

    def __init__(self) -> None:
        self.match_to_return: Match | None = None
        self.recorded: list[IncidentRecord] = []
        self.resolutions: list[tuple[str, str, str]] = []

    def match(self, signature: ProblemSignature, *, threshold: float | None = None) -> Match | None:
        if self.match_to_return is not None:
            return self.match_to_return
        if signature.problem_id == self._SHORT_CIRCUIT_PROBLEM_ID:
            return _build_canned_db_pool_match(signature)
        return None

    def record(self, incident_record: IncidentRecord) -> None:
        self.recorded.append(incident_record)

    def update_resolution(
        self, incident_id: str, *, confirmed_root_cause_key: str, confirmed_fix: str
    ) -> None:
        self.resolutions.append((incident_id, confirmed_root_cause_key, confirmed_fix))


class _DemoPhoenixTracer:  # pragma: no cover
    """In-process stand-in for PhoenixTracer used by the orchestrator.

    The dashboard route reads accuracy data from a real PhoenixTracer
    instance (so ?demo=true returns the canned 41% -> 73% curve); this
    fake is only consumed by Orchestrator.handle() to keep the dispatch
    deterministic.
    """

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


def _build_canned_db_pool_match(signature: ProblemSignature) -> Match:  # pragma: no cover
    """Synthesize a Match for the db_pool_exhaustion fixture.

    Wires wow #3 (memory short-circuit + "seen this 14x" badge) onto a
    live curl. The prior_occurrences=14 is the canonical demo number from
    UNIQUE_IDEA.md; the confirmed_fix is what an on-call engineer would
    actually write after triaging the real incident.
    """
    from datetime import UTC, datetime

    prior_signature = ProblemSignature(
        problem_id="-9223372036854775806_v2_prior_2026Q1",
        title="Response time degradation on payment-service (recurring)",
        severity="PERFORMANCE",
        affected_entity_ids=("SERVICE-payment",),
        affected_entity_types=("SERVICE",),
        opened_at=datetime(2026, 2, 14, 3, 12, tzinfo=UTC),
        fingerprint="db-pool-exhaustion-recurring",
    )
    pool_ev = Evidence(
        specialist="anomaly_window",
        kind="metric_deviation",
        summary="max_connections held at ceiling for 6 minutes during the incident.",
        stance="supports",
        hypothesis_key="db_pool_exhaustion",
        confidence=0.93,
        dynatrace_links=(),
        raw_payload={"metric": "dt.process.db.connections.max", "deviation": 6.0},
    )
    deploy_ev = Evidence(
        specialist="deploy_correlation",
        kind="deploy_window_match",
        summary="Deploy v411 reduced the connection pool size from 100 to 50.",
        stance="supports",
        hypothesis_key="db_pool_exhaustion",
        confidence=0.89,
        dynatrace_links=(),
        raw_payload={"deploy": "v411", "diff": "pool_size: 100 -> 50"},
    )
    prior_brief = Brief(
        problem_id=prior_signature.problem_id,
        generated_at=datetime(2026, 2, 14, 3, 14, tzinfo=UTC),
        ranked_hypotheses=(
            Hypothesis(
                key="db_pool_exhaustion",
                title="Database connection pool exhausted after deploy v411",
                rank=1,
                score=0.91,
                supporting_evidence=(pool_ev, deploy_ev),
                refuting_evidence=(),
                next_action=(
                    "Roll back deploy v411 on payment-service and restore "
                    "the connection pool to its prior size of 100."
                ),
            ),
        ),
        top_recommendation=(
            "Roll back deploy v411 on payment-service and restore the "
            "connection pool to its prior size of 100."
        ),
        unresolved_questions=(),
        from_memory=False,
        pattern_match_score=None,
    )
    prior_record = IncidentRecord(
        incident_id="incident-2026Q1-db-pool-001",
        signature=prior_signature,
        brief=prior_brief,
        opened_at=prior_signature.opened_at,
        resolved_at=datetime(2026, 2, 14, 3, 28, tzinfo=UTC),
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix=(
            "Rolled back deploy v411 and pinned the connection pool size at "
            "100 in payment-service's helm values. Mean time to recover: 14m."
        ),
        embedding=tuple([0.0] * 768),
    )
    return Match(
        record=prior_record,
        similarity=0.92,
        prior_occurrences=14,
    )


def build_demo_dynatrace_client() -> _DemoDynatraceClient:  # pragma: no cover
    """Construct + pre-stub a demo DynatraceClient for the canonical incident."""
    fd = _DemoDynatraceClient()
    sig = make_signature()
    for problem_id in ("-9223372036854775807_v2", "P-001"):
        fd.stub_problem_context(
            problem_id,
            ProblemContext(
                signature=sig,
                impacted_entities=({"id": "SERVICE-ABC123"},),
                events_in_window=(),
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
        "SERVICE-ABC123",
        [
            Entity(
                entity_id="SERVICE-DB",
                entity_type="SERVICE",
                display_name="payments-db",
                distance=1,
            )
        ],
    )
    return fd


def demo_llm_call(_prompt: str) -> dict:  # pragma: no cover
    """Stub Gemini call — returns the canonical hypothesis for the fixture."""
    return {
        "hypotheses": {
            "db_pool_exhaustion": {
                "title": "DB connection pool exhausted by deploy v412",
                "next_action": "Roll back deploy v412 on payment-service.",
            }
        }
    }


__all__ = [
    "Brief",
    "Evidence",
    "Hypothesis",
    "_DemoDynatraceClient",
    "_DemoMemoryStore",
    "_DemoPhoenixTracer",
    "build_demo_dynatrace_client",
    "demo_llm_call",
    "make_signature",
]
