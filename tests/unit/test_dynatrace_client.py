"""TDD spec for DynatraceClient.

The client is the only door to Dynatrace; its behaviors below are
load-bearing for the agent's safety. Real MCP I/O is faked at the
``_MCPProcess`` protocol level so these tests stay deterministic.
"""

from __future__ import annotations

from typing import Any

import pytest

from causal_oncall.domain.exceptions import DynatraceUnavailable, RateLimited
from causal_oncall.dynatrace_client import (
    DQLPlan,
    DynatraceClient,
    DynatraceClientConfig,
)


def _cfg(**overrides) -> DynatraceClientConfig:
    base = dict(
        base_url="https://abc.live.dynatrace.com",
        oauth_client_id="cid",
        oauth_client_secret="sec",
        oauth_token_url="https://sso.dynatrace.com/sso/oauth2/token",
        rate_limit_per_minute=50,
        max_retries=3,
    )
    base.update(overrides)
    return DynatraceClientConfig(**base)


class _ScriptedMCP:
    """Per-test MCP fake. Calls return values in order, then raise."""

    def __init__(self, scripted: list[Any]) -> None:
        self._scripted = list(scripted)
        self.calls: list[tuple[str, dict]] = []

    def call_tool(self, name: str, arguments: dict) -> dict:
        self.calls.append((name, arguments))
        if not self._scripted:
            raise AssertionError(f"MCP fake exhausted on call {name!r}")
        item = self._scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None: ...


def test_get_problem_context_returns_typed_problem_context(monkeypatch):
    mcp = _ScriptedMCP(
        [
            {  # get_problem_details
                "problemId": "P-1",
                "title": "latency spike",
                "severityLevel": "PERFORMANCE",
                "startTime": "2026-05-17T09:30:00Z",
                "affectedEntities": [{"entityId": {"id": "SERVICE-ABC"}, "type": "SERVICE"}],
            },
            {  # impacted-entities DQL
                "records": [{"entity.id": "SERVICE-ABC", "entity.name": "payment"}]
            },
            {"records": [{"event": "deploy", "ts": "2026-05-17T09:25:00Z"}]},  # event-window DQL
        ]
    )
    client = DynatraceClient(_cfg())
    # The implementation under TDD will accept dependency injection of
    # the MCP process; tests rely on that seam.
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    ctx = client.get_problem_context("P-1")
    assert ctx.signature.problem_id == "P-1"
    assert any(e.get("entity.id") == "SERVICE-ABC" for e in ctx.impacted_entities)


def test_execute_dql_retries_on_transient_429_with_exponential_backoff(monkeypatch):
    transient = RateLimited("429", retry_after_seconds=0.01)
    mcp = _ScriptedMCP(
        [
            transient,
            transient,
            {"records": [{"x": 1}]},  # third call succeeds
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    result = client.execute_dql(DQLPlan(query="fetch logs"))
    assert result.rows  # third try produced data
    assert len(mcp.calls) == 3


def test_execute_dql_gives_up_after_max_retries_and_raises_rate_limited(monkeypatch):
    mcp = _ScriptedMCP([RateLimited("429")] * 10)
    client = DynatraceClient(_cfg(max_retries=2))
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    with pytest.raises(RateLimited):
        client.execute_dql(DQLPlan(query="fetch logs"))


def test_execute_dql_maps_unknown_mcp_error_to_dynatrace_unavailable(monkeypatch):
    mcp = _ScriptedMCP([RuntimeError("mcp stdio broken")])
    client = DynatraceClient(_cfg(max_retries=0))
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    with pytest.raises(DynatraceUnavailable):
        client.execute_dql(DQLPlan(query="fetch logs"))


def test_get_topology_neighbors_respects_depth(monkeypatch):
    mcp = _ScriptedMCP(
        [
            {
                "neighbors": [
                    {"entityId": "S1", "type": "SERVICE", "name": "n1", "distance": 1},
                    {"entityId": "S2", "type": "SERVICE", "name": "n2", "distance": 2},
                ]
            }
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    neighbors = client.get_topology_neighbors("SERVICE-ABC", depth=2)
    # All returned entities must have distance <= depth.
    assert all(n.distance <= 2 for n in neighbors)


def test_repeated_get_problem_context_is_cached_within_one_client(monkeypatch):
    mcp = _ScriptedMCP(
        [
            {
                "problemId": "P-1",
                "title": "x",
                "severityLevel": "ERROR",
                "startTime": "2026-05-17T09:30:00Z",
                "affectedEntities": [],
            },
            {"records": []},
            {"records": []},
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    a = client.get_problem_context("P-1")
    b = client.get_problem_context("P-1")
    assert a is b or a == b
    # Only one hydration round-trip even though we asked twice.
    detail_calls = [c for c in mcp.calls if c[0] == "get_problem_details"]
    assert len(detail_calls) == 1


def test_tool_allowlist_rejects_calls_to_unlisted_tools(monkeypatch):
    client = DynatraceClient(_cfg(tool_allowlist=("execute_dql",)))
    # Posting a comment is not in the allowlist — must refuse rather than
    # silently call into the MCP server with a banned tool name.
    with pytest.raises(PermissionError):
        client.post_problem_comment("P-1", "hello")


def test_execute_dql_returns_empty_result_when_mcp_records_are_empty(monkeypatch):
    mcp = _ScriptedMCP([{"records": []}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    result = client.execute_dql(DQLPlan(query="fetch logs | limit 0"))
    assert result.columns == ()
    assert result.rows == ()
    assert result.execution_ms == 0


def test_execute_dql_handles_prose_only_envelope_as_empty_result(monkeypatch):
    """Live Dynatrace MCP (v1.8.5) returns a markdown prose envelope when a DQL
    query scans zero records (W2-S0 finding from the empty trial tenant).

    The client must collapse that envelope to an empty QueryResult without
    raising — specialists tolerate empty evidence, but they choke on a
    parser exception.
    """
    mcp = _ScriptedMCP([{"raw": "Scanned 0 records; rendered by MCP UI."}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    result = client.execute_dql(DQLPlan(query="fetch logs | limit 1"))
    assert result.columns == ()
    assert result.rows == ()
    assert result.execution_ms == 0


def test_execute_dql_passes_dqlStatement_as_the_arg_key_to_mcp(monkeypatch):
    """Pin the MCP arg-shape contract: execute_dql must pass ``dqlStatement``.

    Regression guard against the W2-S0 drift where prior code passed
    ``{"query": ..., "parameters": ...}`` and the live MCP rejected
    every call. If the upstream MCP renames the arg again, this test
    fails loudly before reaching the cassette suite.
    """
    mcp = _ScriptedMCP([{"records": []}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    client.execute_dql(DQLPlan(query="fetch logs | limit 1"))
    assert mcp.calls == [("execute_dql", {"dqlStatement": "fetch logs | limit 1"})]


def test_get_topology_neighbors_drops_neighbors_exceeding_depth(monkeypatch):
    mcp = _ScriptedMCP(
        [
            {
                "neighbors": [
                    {"entityId": "S1", "type": "SERVICE", "name": "n1", "distance": 1},
                    {"entityId": "S99", "type": "SERVICE", "name": "n99", "distance": 99},
                ]
            }
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    neighbors = client.get_topology_neighbors("SERVICE-ABC", depth=1)
    assert [n.entity_id for n in neighbors] == ["S1"]


def test_post_problem_comment_returns_comment_id_from_mcp_response(monkeypatch):
    mcp = _ScriptedMCP([{"commentId": "C-42"}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    comment_id = client.post_problem_comment("P-1", "diagnosis")
    assert comment_id == "C-42"


def test_close_invokes_underlying_mcp_close_and_clears_cache(monkeypatch):
    """Idempotent + safe even if invoked twice."""

    class _CloseMCP:
        def __init__(self) -> None:
            self.closed = 0
            self.calls: list[tuple[str, dict]] = []

        def call_tool(self, name: str, arguments: dict) -> dict:
            self.calls.append((name, arguments))
            return {
                "problemId": "P-1",
                "title": "x",
                "severityLevel": "ERROR",
                "startTime": "2026-05-17T09:30:00Z",
                "affectedEntities": [],
            }

        def close(self) -> None:
            self.closed += 1

    mcp = _CloseMCP()
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    # Prime the cache so .close() also clears it.
    monkeypatch.setattr(
        client,
        "_problem_context_cache",
        {"P-1": object()},
        raising=False,
    )
    client.close()
    assert mcp.closed == 1
    # Second call is a no-op (idempotent).
    client.close()
    assert mcp.closed == 1


def test_tool_allowlist_permits_listed_methods(monkeypatch):
    """When the requested tool is in the allowlist, the call proceeds normally."""
    mcp = _ScriptedMCP([{"records": [{"x": 1}]}])
    client = DynatraceClient(_cfg(tool_allowlist=("execute_dql",)))
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    result = client.execute_dql(DQLPlan(query="fetch logs | limit 1"))
    assert result.rows == ((1,),)


def test_execute_dql_propagates_domain_dynatrace_unavailable_unchanged(monkeypatch):
    """Tools that already raise DynatraceUnavailable surface without rewrapping."""
    mcp = _ScriptedMCP([DynatraceUnavailable("upstream gone")])
    client = DynatraceClient(_cfg(max_retries=0))
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    with pytest.raises(DynatraceUnavailable, match="upstream gone"):
        client.execute_dql(DQLPlan(query="fetch logs"))
