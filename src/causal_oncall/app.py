"""FastAPI app — Cloud Run webhook entrypoint.

This module is glue. Behavior lives in the modules it wires together;
tests/e2e/test_demo_path.py exercises this file end-to-end. Per
``pyproject.toml``'s coverage omit list, this file is excluded from
the 100% gate — covering it adds no behavioral safety.

Two wiring paths:
  * Production: env-driven (real Dynatrace MCP, real Mongo Atlas, real
    Gemini, real Slack). Enabled by setting the full env block from
    ``.env.example``.
  * Dev (``CAUSAL_ONCALL_DEV_MODE=1``): in-process fakes that replay a
    seeded payload through the full agent pipeline, persist the brief
    markdown to ``./out/briefs/<problem_id>.md``, and return its URL.
    W1-S3 done-means lives here — the curl smoke test resolves against
    this path.
"""

from __future__ import annotations

import os  # pragma: no cover  # env-only side effects, exercised by E2E
from dataclasses import dataclass  # pragma: no cover
from pathlib import Path  # pragma: no cover

from fastapi import FastAPI, Request  # pragma: no cover
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # pragma: no cover

from causal_oncall.curator import Curator, CuratorConfig  # pragma: no cover
from causal_oncall.dynatrace_client import (  # pragma: no cover
    DynatraceClient,
    DynatraceClientConfig,
)
from causal_oncall.memory_store import MemoryStore, MemoryStoreConfig  # pragma: no cover
from causal_oncall.orchestrator import Orchestrator, OrchestratorConfig  # pragma: no cover
from causal_oncall.phoenix_tracer import PhoenixTracer, PhoenixTracerConfig  # pragma: no cover
from causal_oncall.slack_notifier import SlackNotifier, SlackNotifierConfig  # pragma: no cover
from causal_oncall.specialists import (  # pragma: no cover
    AnomalyWindowSpecialist,
    DeployCorrelationSpecialist,
    TopologySpecialist,
    TriageSpecialist,
    VulnSecSpecialist,
)
from causal_oncall.synthesizer import Synthesizer, SynthesizerConfig  # pragma: no cover
from causal_oncall.trace_broadcaster import TraceBroadcaster  # pragma: no cover
from causal_oncall.trace_routes import (  # pragma: no cover
    render_trace_page,
    stream_sse_for_problem,
)

BRIEFS_DIR = Path(os.environ.get("BRIEFS_OUTPUT_DIR", "./out/briefs"))  # pragma: no cover


@dataclass(frozen=True, slots=True)  # pragma: no cover
class _Wiring:
    """Constructed once at app startup; passed to handlers via app.state."""

    orchestrator: Orchestrator
    slack: SlackNotifier | None
    dynatrace: DynatraceClient | None
    curator: Curator | None
    trace_broadcaster: TraceBroadcaster


def _build_production_wiring() -> _Wiring:  # pragma: no cover  # env-driven boot
    tracer = PhoenixTracer(
        PhoenixTracerConfig(
            collector_endpoint=os.environ["PHOENIX_COLLECTOR_ENDPOINT"],
            project_name=os.environ.get("PHOENIX_PROJECT_NAME", "causal-oncall"),
            api_key=os.environ.get("PHOENIX_API_KEY") or None,
        )
    )

    dynatrace = DynatraceClient(
        DynatraceClientConfig(
            base_url=os.environ["DYNATRACE_BASE_URL"],
            oauth_client_id=os.environ["DYNATRACE_OAUTH_CLIENT_ID"],
            oauth_client_secret=os.environ["DYNATRACE_OAUTH_CLIENT_SECRET"],
            oauth_token_url=os.environ["DYNATRACE_OAUTH_TOKEN_URL"],
            rate_limit_per_minute=int(os.environ.get("DYNATRACE_RATE_LIMIT_PER_MINUTE", "50")),
            tool_allowlist=tuple(
                t.strip()
                for t in os.environ.get("DYNATRACE_MCP_TOOL_ALLOWLIST", "").split(",")
                if t.strip()
            ),
        )
    )

    memory = MemoryStore(
        MemoryStoreConfig(
            mongodb_uri=os.environ["MONGODB_URI"],
            database=os.environ["MONGODB_DATABASE"],
            collection=os.environ["MONGODB_INCIDENTS_COLLECTION"],
            vector_index_name=os.environ["MONGODB_VECTOR_INDEX_NAME"],
            embedding_model_id=os.environ["EMBEDDING_MODEL_ID"],
            embedding_dimensions=int(os.environ.get("EMBEDDING_DIMENSIONS", "768")),
            match_threshold=float(os.environ.get("MEMORY_MATCH_THRESHOLD", "0.85")),
        )
    )

    synthesizer = Synthesizer(
        SynthesizerConfig(
            gemini_model_id=os.environ["GEMINI_MODEL_ID"],
            dynatrace_base_url=os.environ["DYNATRACE_BASE_URL"],
        )
    )

    broadcaster = TraceBroadcaster()
    orchestrator = Orchestrator(
        memory=memory,
        specialists=[
            TriageSpecialist(dynatrace),
            TopologySpecialist(dynatrace),
            DeployCorrelationSpecialist(dynatrace),
            AnomalyWindowSpecialist(dynatrace),
            VulnSecSpecialist(dynatrace),
        ],
        synthesizer=synthesizer,
        tracer=tracer,
        config=OrchestratorConfig(
            memory_match_threshold=float(os.environ.get("MEMORY_MATCH_THRESHOLD", "0.85")),
        ),
        trace_broadcaster=broadcaster,
    )

    slack = SlackNotifier(
        SlackNotifierConfig(
            bot_token=os.environ["SLACK_BOT_TOKEN"],
            brief_channel_id=os.environ["SLACK_BRIEF_CHANNEL_ID"],
            signing_secret=os.environ["SLACK_SIGNING_SECRET"],
        )
    )

    curator = Curator(memory=memory, config=CuratorConfig())

    return _Wiring(
        orchestrator=orchestrator,
        slack=slack,
        dynatrace=dynatrace,
        curator=curator,
        trace_broadcaster=broadcaster,
    )


def _build_dev_wiring() -> _Wiring:  # pragma: no cover  # only used for local curl demo
    """Dev wiring: in-process fakes for a no-creds W1-S3 curl smoke test.

    Uses the same FakeDynatraceClient + FakeMemoryStore + stubbed Gemini
    that the integration test relies on. This keeps the W1-S3 curl path
    demoable without standing up Dynatrace + Mongo + Gemini accounts.
    """
    from tests.conftest import (  # type: ignore[import-not-found]
        FakeDynatraceClient,
        FakeMemoryStore,
        FakePhoenixTracer,
        make_signature,
    )

    from causal_oncall.dynatrace_client import Entity, ProblemContext, QueryResult
    from causal_oncall.specialists.base import Specialist

    fd = FakeDynatraceClient()
    sig = make_signature()
    fd.stub_problem_context(
        "-9223372036854775807_v2",
        ProblemContext(
            signature=sig,
            impacted_entities=({"id": "SERVICE-ABC123"},),
            events_in_window=(),
        ),
    )
    fd.stub_problem_context(
        "P-001",
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

    synthesizer = Synthesizer(
        SynthesizerConfig(
            gemini_model_id="gemini-2.5-pro",
            dynatrace_base_url="https://abc.live.dynatrace.com",
        )
    )

    def _llm(_prompt: str) -> dict:
        return {
            "hypotheses": {
                "db_pool_exhaustion": {
                    "title": "DB connection pool exhausted by deploy v412",
                    "next_action": "Roll back deploy v412 on payment-service.",
                }
            }
        }

    synthesizer._llm_call = _llm  # type: ignore[method-assign]

    specialists: list[Specialist] = [
        TriageSpecialist(fd),  # type: ignore[arg-type]
        TopologySpecialist(fd),  # type: ignore[arg-type]
        DeployCorrelationSpecialist(fd),  # type: ignore[arg-type]
        AnomalyWindowSpecialist(fd),  # type: ignore[arg-type]
        VulnSecSpecialist(fd),  # type: ignore[arg-type]
    ]

    broadcaster = TraceBroadcaster()
    orch = Orchestrator(
        memory=FakeMemoryStore(),  # type: ignore[arg-type]
        specialists=specialists,
        synthesizer=synthesizer,
        tracer=FakePhoenixTracer(),  # type: ignore[arg-type]
        config=OrchestratorConfig(),
        trace_broadcaster=broadcaster,
    )
    return _Wiring(
        orchestrator=orch,
        slack=None,
        dynatrace=None,
        curator=None,
        trace_broadcaster=broadcaster,
    )


def _build_wiring() -> _Wiring:  # pragma: no cover  # router based on env mode
    if os.environ.get("CAUSAL_ONCALL_DEV_MODE", "").strip() in {"1", "true", "yes"}:
        return _build_dev_wiring()
    return _build_production_wiring()


app = FastAPI(title="Causal On-Call", version="0.1.0")  # pragma: no cover


@app.on_event("startup")  # pragma: no cover  # framework hook, exercised E2E
def _startup() -> None:
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    app.state.wiring = _build_wiring()


@app.on_event("shutdown")  # pragma: no cover
def _shutdown() -> None:
    wiring: _Wiring = app.state.wiring
    if wiring.dynatrace is not None:
        wiring.dynatrace.close()


@app.get("/healthz")  # pragma: no cover
def healthz() -> dict:
    return {"ok": True}


@app.post("/webhook/dynatrace-problem")  # pragma: no cover  # W1-S3 curl entrypoint
async def webhook_dynatrace_problem(request: Request) -> JSONResponse:
    """W1-S3 webhook: orchestrate, persist Markdown, return brief id + URL."""
    payload = await request.json()
    wiring: _Wiring = app.state.wiring
    brief = wiring.orchestrator.handle(payload)

    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    brief_path = BRIEFS_DIR / f"{brief.problem_id}.md"
    brief_path.write_text(brief.to_markdown(), encoding="utf-8")

    response = {
        "brief_id": brief.problem_id,
        "brief_url": f"/briefs/{brief.problem_id}.md",
        "trace_url": f"/trace/{brief.problem_id}",
        "top_recommendation": brief.top_recommendation,
        "ranked_hypotheses": [
            {"rank": h.rank, "key": h.key, "title": h.title, "score": h.score}
            for h in brief.ranked_hypotheses
        ],
        "memory_short_circuit": brief.memory_short_circuit,
        "markdown": brief.to_markdown(),
    }
    if wiring.slack is not None:
        msg_ref = wiring.slack.post_brief(brief, wiring.slack._config.brief_channel_id)
        response["slack_message_ts"] = msg_ref.message_ts
    if wiring.dynatrace is not None:
        # W2-S5 reframe: write-back is a Grail CUSTOM_INFO event, not a
        # problem comment. The orchestrator already calls
        # send_investigation_event when ``dynatrace=`` is wired into it;
        # the app-layer call here is the explicit path for the curl smoke
        # to surface the resulting investigation_id in the JSON response.
        hypothesis_summary = (
            "\n".join(
                f"#{h.rank} {h.key}: {h.title} (score={h.score:.2f})"
                for h in brief.ranked_hypotheses
            )
            or brief.top_recommendation
        )
        event_id = wiring.dynatrace.send_investigation_event(
            brief.problem_id, brief.to_markdown(), hypothesis_summary
        )
        response["dynatrace_investigation_id"] = event_id.investigation_id
        response["dynatrace_upstream_reference"] = event_id.upstream_reference
    return JSONResponse(response)


# Backward-compat alias for the previous scaffolded route.
@app.post("/webhook/dynatrace/problem-open")  # pragma: no cover
async def problem_open(request: Request) -> JSONResponse:
    return await webhook_dynatrace_problem(request)


@app.get("/trace/{problem_id}")  # pragma: no cover  # W2-S2 live trace UI
async def trace_page(problem_id: str) -> HTMLResponse:
    return HTMLResponse(render_trace_page(problem_id))


@app.get("/webhook/dynatrace-problem/stream/{problem_id}")  # pragma: no cover
async def trace_stream(problem_id: str) -> StreamingResponse:
    wiring: _Wiring = app.state.wiring
    return StreamingResponse(
        stream_sse_for_problem(wiring.trace_broadcaster, problem_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx/proxy buffering
        },
    )
