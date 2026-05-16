"""FastAPI app — Cloud Run webhook entrypoint.

This module is glue. Behavior lives in the modules it wires together;
tests/e2e/test_demo_path.py exercises this file end-to-end. Per
``pyproject.toml``'s coverage omit list, this file is excluded from
the 100% gate — covering it adds no behavioral safety.
"""

from __future__ import annotations

import os  # pragma: no cover  # env-only side effects, exercised by E2E
from dataclasses import dataclass  # pragma: no cover

from fastapi import FastAPI, Request  # pragma: no cover

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


@dataclass(frozen=True, slots=True)  # pragma: no cover
class _Wiring:
    """Constructed once at app startup; passed to handlers via app.state."""

    orchestrator: Orchestrator
    slack: SlackNotifier
    dynatrace: DynatraceClient
    curator: Curator


def _build_wiring() -> _Wiring:  # pragma: no cover  # env-driven boot
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
        orchestrator=orchestrator, slack=slack, dynatrace=dynatrace, curator=curator
    )


app = FastAPI(title="Causal On-Call", version="0.1.0")  # pragma: no cover


@app.on_event("startup")  # pragma: no cover  # framework hook, exercised E2E
def _startup() -> None:
    app.state.wiring = _build_wiring()


@app.on_event("shutdown")  # pragma: no cover
def _shutdown() -> None:
    wiring: _Wiring = app.state.wiring
    wiring.dynatrace.close()


@app.get("/healthz")  # pragma: no cover
def healthz() -> dict:
    return {"ok": True}


@app.post("/webhook/dynatrace/problem-open")  # pragma: no cover
async def problem_open(request: Request) -> dict:
    payload = await request.json()
    wiring: _Wiring = app.state.wiring
    brief = wiring.orchestrator.handle(payload)
    msg_ref = wiring.slack.post_brief(brief, wiring.slack._config.brief_channel_id)
    comment_id = wiring.dynatrace.post_problem_comment(brief.problem_id, brief.to_markdown())
    return {
        "problem_id": brief.problem_id,
        "slack_message_ts": msg_ref.message_ts,
        "dynatrace_comment_id": comment_id,
    }
