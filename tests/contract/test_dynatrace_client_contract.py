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
    """Replay the recorded ``execute_dql`` cassette and validate the typed result."""
    mcp = CassetteMCP(
        cassette_path("test_execute_dql_against_real_mcp_returns_a_valid_query_result")
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    result = client.execute_dql(DQLPlan(query="fetch logs | limit 1"))

    assert isinstance(result, QueryResult)
    assert result.columns, "columns must be inferred from the first record"
    assert len(result.rows) == 1
    assert result.execution_ms >= 0
    assert mcp.calls[0][0] == "execute_dql"


def test_get_problem_context_handles_known_test_problem_id(monkeypatch):
    """Replay the recorded ``get_problem_context`` cassette and validate the hydration."""
    mcp = CassetteMCP(cassette_path("test_get_problem_context_handles_known_test_problem_id"))
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    ctx = client.get_problem_context("PROBLEM-CASSETTE-001")

    assert isinstance(ctx, ProblemContext)
    assert isinstance(ctx.signature, ProblemSignature)
    assert ctx.signature.problem_id == "PROBLEM-CASSETTE-001"
    assert ctx.impacted_entities, "impacted entities must hydrate from the events DQL"
    # The cassette includes a DEPLOY event — verify it landed in the window.
    assert any(any(value == "DEPLOY" for value in event.values()) for event in ctx.events_in_window)
    # Tool-call sequence: 1 get_problem_details + 2 execute_dql hydrations.
    tool_names = [name for name, _ in mcp.calls]
    assert tool_names == ["get_problem_details", "execute_dql", "execute_dql"]


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
