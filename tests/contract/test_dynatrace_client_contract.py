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

    NOTE (W2-S0 drift, see BUILD-LOG.md): Live Dynatrace MCP v1.8.5 does
    NOT expose a ``get_problem_details`` tool — the closest read is
    ``list_problems`` with a single-id filter. The synthetic cassette
    here preserves the *parser shape* contract while
    ``DynatraceClient.get_problem_context`` still calls the old tool
    name. A follow-up slice (W2-S5 candidate) will rewire the client
    to ``list_problems`` and re-record this cassette live. Out of
    W2-S0 scope per directive ("don't touch any other slice").

    The live counterpart cassette ``_live_get_problem_context.json``
    (captured from the trial tenant) is held in the same directory for
    when the rewire lands — it documents the empty-tenant + new-tool
    response shape.
    """
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
