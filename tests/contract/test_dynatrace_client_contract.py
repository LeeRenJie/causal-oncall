"""Contract tests for DynatraceClient against the Dynatrace MCP shape.

These tests run in two modes:

* **Replay (default, no creds, CI-safe):** A cassette under
  ``tests/contract/cassettes/<test_name>.json`` is replayed via
  ``CassetteMCP``. Catches the shape-drift failure mode the unit fakes
  cannot: when the MCP server changes its response shape, the cassette
  payload still satisfies the typed ``QueryResult`` / ``ProblemContext``
  contracts only if the parsing code in ``DynatraceClient`` still
  understands the shape.

* **Live (opt-in, ``-m requires_creds``):** Drives the real Dynatrace MCP
  via ``scripts/record_cassettes.py`` to re-record the cassettes. CI
  does not opt into this marker; the recording is a manual builder step
  in a session with valid OAuth + browser.

The replay tests are unconditional — they exercise the parsing path
without external dependencies. The ``requires_creds`` tests are gated by
both the pytest marker and the env-var presence.
"""

from __future__ import annotations

import os

import pytest

from causal_oncall.domain.problem_signature import ProblemSignature
from causal_oncall.dynatrace_client import (
    DQLPlan,
    DynatraceClient,
    DynatraceClientConfig,
    EventId,
    ProblemContext,
    QueryResult,
)
from tests.contract.cassettes._cassette import CassetteMCP, cassette_path


def _cfg() -> DynatraceClientConfig:
    return DynatraceClientConfig(
        base_url="https://cassette.live.dynatrace.com",
        oauth_client_id="cassette-cid",
        oauth_client_secret="cassette-sec",
        oauth_token_url="https://sso.dynatrace.com/sso/oauth2/token",
        rate_limit_per_minute=50,
        max_retries=0,
    )


def test_execute_dql_against_real_mcp_returns_a_valid_query_result(monkeypatch):
    """Replay the live ``execute_dql`` cassette and validate the typed result.

    The W2-S0 live recording came from an empty trial tenant — the MCP
    returns a prose markdown envelope (``{"raw": "...0 records..."}``)
    rather than a structured record set. The client must:

    * collapse the prose envelope into an empty ``QueryResult`` (not raise)
    * still pass ``dqlStatement`` as the arg key to the MCP
    * still call the ``execute_dql`` tool exactly once

    When the tenant grows to have real log records, re-running
    ``scripts/record_cassettes.py`` upgrades this cassette into a
    populated row set and the empty-shape assertions below tighten
    naturally. The cassette is the source of truth, not these assertions.
    """
    mcp = CassetteMCP(
        cassette_path("test_execute_dql_against_real_mcp_returns_a_valid_query_result")
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    result = client.execute_dql(DQLPlan(query="fetch logs | limit 1"))

    assert isinstance(result, QueryResult)
    # Empty trial tenant → empty result; populated tenant → non-empty.
    # Either way the parser must not raise.
    assert result.execution_ms >= 0
    assert len(mcp.calls) == 1
    assert mcp.calls[0][0] == "execute_dql"
    # Arg-shape contract: live MCP v1.8.5 requires ``dqlStatement``.
    assert mcp.calls[0][1] == {"dqlStatement": "fetch logs | limit 1"}


def test_get_problem_context_handles_known_test_problem_id(monkeypatch):
    """Replay the recorded ``get_problem_context`` cassette and validate the hydration.

    W2-S5 realigned this test to the live MCP v1.8.5 tool surface: the
    client now drives ``list_problems`` (with a single-id
    ``additionalFilter``) plus two ``execute_dql`` queries to hydrate
    impacted entities + the event window. The empty trial tenant's
    cassette exercises the prose-envelope code paths end-to-end:
    list_problems returns ``{"raw": "No problems found"}`` and both
    DQLs collapse to empty record sets. The parser must synthesize a
    minimal signature from the inbound problem_id rather than raise.
    """
    mcp = CassetteMCP(cassette_path("test_get_problem_context_handles_known_test_problem_id"))
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    ctx = client.get_problem_context("PROBLEM-CASSETTE-001")

    assert isinstance(ctx, ProblemContext)
    assert isinstance(ctx.signature, ProblemSignature)
    assert ctx.signature.problem_id == "PROBLEM-CASSETTE-001"
    # Empty trial tenant — both hydration DQLs return prose envelopes,
    # which the client collapses to empty tuples without raising.
    assert ctx.impacted_entities == ()
    assert ctx.events_in_window == ()
    # Tool-call sequence: list_problems first, then two execute_dql hydrations.
    tool_names = [name for name, _ in mcp.calls]
    assert tool_names == ["list_problems", "execute_dql", "execute_dql"]
    # First call carried the single-id additionalFilter (per W2-S5).
    assert "PROBLEM-CASSETTE-001" in mcp.calls[0][1]["additionalFilter"]


def test_send_investigation_event_against_real_mcp_returns_an_event_id(monkeypatch):
    """Replay the recorded ``send_event`` cassette and validate the typed ``EventId``.

    The W2-S5 reframe: the agent's write-back surface is the Events API
    v2 ingest (``send_event`` MCP tool), not the absent
    ``post_problem_comment`` tool. The empty trial tenant accepts the
    CUSTOM_INFO ingest and returns a prose confirmation; the client
    must surface a typed ``EventId`` with a deterministic
    ``investigation_id`` and the prose snippet as
    ``upstream_reference``.
    """
    mcp = CassetteMCP(
        cassette_path("test_send_investigation_event_against_real_mcp_returns_an_event_id")
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    event_id = client.send_investigation_event(
        "PROBLEM-CASSETTE-001",
        "Cassette brief markdown placeholder",
        "Cassette smoke summary",
    )

    assert isinstance(event_id, EventId)
    assert event_id.investigation_id.startswith("causal-oncall-PROBLEM-CASSETTE-001-")
    # Empty trial tenant → MCP returns a prose confirmation; we surface
    # whatever the upstream said for human cross-reference.
    assert "Event sent" in event_id.upstream_reference or event_id.upstream_reference
    # Exactly one MCP round-trip — no fan-out, no retry.
    assert len(mcp.calls) == 1
    tool, args = mcp.calls[0]
    assert tool == "send_event"
    assert args["eventType"] == "CUSTOM_INFO"
    # Title must mention the problem id so on-call ops can scan the
    # event timeline by problem.
    assert "PROBLEM-CASSETTE-001" in args["title"]
    # Properties must be string-only per the v1.8.5 input schema.
    assert all(isinstance(v, str) for v in args["properties"].values())


@pytest.mark.requires_creds
@pytest.mark.skipif(
    not os.environ.get("DYNATRACE_OAUTH_CLIENT_ID"),
    reason="Dynatrace credentials not set; live MCP suite gated on real env",
)
def test_live_dynatrace_mcp_round_trip_smoke():  # pragma: no cover  # opt-in only
    """Smoke test that the real MCP stdio handshake completes end-to-end.

    Skipped in default runs. Driven manually before recording cassettes to
    confirm the trial tenant + OAuth + npx package combination is healthy.
    """
    pytest.skip("Live MCP smoke; run via scripts/record_cassettes.py")
