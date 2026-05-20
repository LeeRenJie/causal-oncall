"""FastAPI app — Cloud Run webhook entrypoint.

This module is glue. Behavior lives in the modules it wires together;
tests/e2e/test_demo_path.py exercises this file end-to-end. Per
``pyproject.toml``'s coverage omit list, this file is excluded from
the 100% gate — covering it adds no behavioral safety.

Two wiring paths:
  * Production: env-driven (real Dynatrace MCP, real Mongo Atlas, real
    Gemini, real Slack). Enabled by setting the full env block from
    ``.env.example``.
  * Dev / demo (``CAUSAL_ONCALL_DEV_MODE=1`` or
    ``CAUSAL_ONCALL_DEMO_MODE=true``): in-process fakes from
    ``_demo_wiring.py`` that replay a seeded payload through the full
    agent pipeline, persist the brief markdown to
    ``./out/briefs/<problem_id>.md``, and return its URL. W1-S3 done-means
    lives here (curl smoke). W4-S1 Cloud Run uses the DEMO_MODE gate as
    the judges' demo URL while Dynatrace OAuth client setup is pending.
"""

from __future__ import annotations

import os  # pragma: no cover  # env-only side effects, exercised by E2E
from dataclasses import dataclass  # pragma: no cover
from pathlib import Path  # pragma: no cover

from fastapi import FastAPI, Request  # pragma: no cover
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse  # pragma: no cover

from causal_oncall.curator import Curator, CuratorConfig  # pragma: no cover
from causal_oncall.dashboard import (  # pragma: no cover
    dashboard_payload_from,
    demo_dashboard_payload,
    render_dashboard_page,
)
from causal_oncall.dynatrace_client import (  # pragma: no cover
    DynatraceClient,
    DynatraceClientConfig,
)
from causal_oncall.memory_store import MemoryStore, MemoryStoreConfig  # pragma: no cover
from causal_oncall.orchestrator import Orchestrator, OrchestratorConfig  # pragma: no cover
from causal_oncall.phoenix_tracer import PhoenixTracer  # pragma: no cover
from causal_oncall.phoenix_tracer import (
    config_from_env as _phoenix_config_from_env,  # pragma: no cover
)
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
    tracer: PhoenixTracer


def _build_production_wiring() -> _Wiring:  # pragma: no cover  # env-driven boot
    # W3-S4: ``config_from_env`` resolves all PHOENIX_* vars including
    # the outcome-store path. Real Arize Phoenix SDK kicks in when
    # ``PHOENIX_COLLECTOR_ENDPOINT`` is set; otherwise the stdout
    # fallback recorder runs (preserves W1 local-dev behavior).
    tracer = PhoenixTracer(_phoenix_config_from_env())

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
        tracer=tracer,
    )


def _build_dev_wiring() -> _Wiring:  # pragma: no cover  # only used for local curl demo
    """Dev/demo wiring: in-process fakes for the no-creds curl + Cloud Run demo path.

    W1-S3: local curl smoke (`CAUSAL_ONCALL_DEV_MODE=1`).
    W4-S1: Cloud Run live demo URL (`CAUSAL_ONCALL_DEMO_MODE=true`),
           when Dynatrace OAuth client credentials are not yet
           available — see BUILD-LOG.md W4-S1.

    The fakes live in ``_demo_wiring.py`` under ``src/`` so the Docker
    image (which excludes ``tests/``) can boot this path. Production
    wiring path (real Dynatrace MCP, Mongo Atlas, Gemini, Slack) lives
    in ``_build_production_wiring`` and stays unchanged.
    """
    from causal_oncall._demo_wiring import (
        _DemoMemoryStore,
        _DemoPhoenixTracer,
        build_demo_dynatrace_client,
        demo_llm_call,
    )
    from causal_oncall.specialists.base import Specialist

    fd = build_demo_dynatrace_client()

    synthesizer = Synthesizer(
        SynthesizerConfig(
            gemini_model_id="gemini-2.5-pro",
            dynatrace_base_url="https://abc.live.dynatrace.com",
        )
    )
    synthesizer._llm_call = demo_llm_call  # type: ignore[method-assign]

    specialists: list[Specialist] = [
        TriageSpecialist(fd),  # type: ignore[arg-type]
        TopologySpecialist(fd),  # type: ignore[arg-type]
        DeployCorrelationSpecialist(fd),  # type: ignore[arg-type]
        AnomalyWindowSpecialist(fd),  # type: ignore[arg-type]
        VulnSecSpecialist(fd),  # type: ignore[arg-type]
    ]

    broadcaster = TraceBroadcaster()
    # W3-S5: the dashboard route reads accuracy data from a real
    # PhoenixTracer. Dev/demo mode points at an empty JSONL outcome
    # store (stdout fallback recorder); the live-demo path uses
    # ``/dashboard?demo=true`` which never touches this tracer.
    dev_tracer = PhoenixTracer(_phoenix_config_from_env())
    orch = Orchestrator(
        memory=_DemoMemoryStore(),  # type: ignore[arg-type]
        specialists=specialists,
        synthesizer=synthesizer,
        tracer=_DemoPhoenixTracer(),  # type: ignore[arg-type]
        config=OrchestratorConfig(),
        trace_broadcaster=broadcaster,
    )
    # Slack is opt-in even in demo mode: if SLACK_BOT_TOKEN is set we wire
    # the real SlackNotifier so briefs flow to the configured channel during
    # the recording. Mongo + Dynatrace stay faked because they're heavier to
    # bootstrap, but Slack is a single API call per brief — no state, no
    # subprocess. Falls back to None if any of the 3 env vars is missing.
    slack: SlackNotifier | None = None
    if os.environ.get("SLACK_BOT_TOKEN") and os.environ.get(
        "SLACK_SIGNING_SECRET"
    ) and os.environ.get("SLACK_BRIEF_CHANNEL_ID"):
        slack = SlackNotifier(
            SlackNotifierConfig(
                bot_token=os.environ["SLACK_BOT_TOKEN"],
                brief_channel_id=os.environ["SLACK_BRIEF_CHANNEL_ID"],
                signing_secret=os.environ["SLACK_SIGNING_SECRET"],
            )
        )
    return _Wiring(
        orchestrator=orch,
        slack=slack,
        dynatrace=None,
        curator=None,
        trace_broadcaster=broadcaster,
        tracer=dev_tracer,
    )


def _build_wiring() -> _Wiring:  # pragma: no cover  # router based on env mode
    # W4-S1: both ``CAUSAL_ONCALL_DEV_MODE`` (legacy local-dev gate)
    # and ``CAUSAL_ONCALL_DEMO_MODE`` (Cloud Run live demo gate) route
    # to the in-process demo wiring. Either being truthy is enough.
    truthy = {"1", "true", "yes"}
    if (
        os.environ.get("CAUSAL_ONCALL_DEV_MODE", "").strip().lower() in truthy
        or os.environ.get("CAUSAL_ONCALL_DEMO_MODE", "").strip().lower() in truthy
    ):
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
        # W3-S2: explicit fields the demo + dashboard read for the
        # "pre-flight memory hit" wow moment. Kept alongside the legacy
        # `memory_short_circuit` boolean so consumers can migrate gradually.
        "from_memory": brief.from_memory,
        "pattern_match_score": brief.pattern_match_score,
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


@app.get("/dashboard")  # pragma: no cover  # W3-S5 self-improvement dashboard UI
async def dashboard_page() -> HTMLResponse:
    """Render the single-page self-improvement dashboard.

    The page calls back to ``GET /dashboard/data`` (optionally with
    ``?demo=true``) every 30s via vanilla ``fetch`` + ``setInterval``.
    """
    return HTMLResponse(render_dashboard_page())


@app.get("/dashboard/data")  # pragma: no cover  # W3-S5 JSON data binding
async def dashboard_data(demo: bool = False) -> JSONResponse:
    """Return the dashboard payload as JSON.

    With ``?demo=true`` returns the hand-crafted 41% -> 73% curve so the
    3-minute live demo lands the wow moment without 6 months of real
    history. Without it, returns a snapshot of the real
    :class:`PhoenixTracer` outcome store.
    """
    if demo:
        return JSONResponse(demo_dashboard_payload().to_dict())
    wiring: _Wiring = app.state.wiring
    return JSONResponse(dashboard_payload_from(wiring.tracer).to_dict())
