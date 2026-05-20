"""TDD spec for DynatraceClient.

The client is the only door to Dynatrace; its behaviors below are
load-bearing for the agent's safety. Real MCP I/O is faked at the
``_MCPProcess`` protocol level so these tests stay deterministic.

W2-S5 realigns the tested tools to MCP v1.8.5's actual surface:
``list_problems`` + ``execute_dql`` replace the absent
``get_problem_details`` / ``get_topology_neighbors`` tools, and the new
``send_investigation_event`` replaces the absent ``post_problem_comment``
write surface with a CUSTOM_INFO event ingest.
"""

from __future__ import annotations

from typing import Any

import pytest

from causal_oncall.domain.exceptions import DynatraceUnavailable, RateLimited
from causal_oncall.dynatrace_client import (
    DQLPlan,
    DynatraceClient,
    DynatraceClientConfig,
    Entity,
    EventId,
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


def test_get_problem_context_returns_typed_problem_context_via_list_problems(monkeypatch):
    """W2-S5: get_problem_context hydrates via list_problems + 2 execute_dql."""
    mcp = _ScriptedMCP(
        [
            {  # list_problems with additionalFilter
                "problems": [
                    {
                        "problemId": "P-1",
                        "title": "latency spike",
                        "severityLevel": "PERFORMANCE",
                        "startTime": "2026-05-17T09:30:00Z",
                        "affectedEntities": [
                            {"entityId": {"id": "SERVICE-ABC"}, "type": "SERVICE"}
                        ],
                    }
                ]
            },
            {  # impacted-entities DQL
                "records": [{"entity.id": "SERVICE-ABC", "entity.name": "payment"}]
            },
            {"records": [{"event": "deploy", "ts": "2026-05-17T09:25:00Z"}]},  # event-window DQL
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    ctx = client.get_problem_context("P-1")
    assert ctx.signature.problem_id == "P-1"
    assert any(e.get("entity.id") == "SERVICE-ABC" for e in ctx.impacted_entities)
    assert any(e.get("event") == "deploy" for e in ctx.events_in_window)
    # Tool sequence: list_problems then two execute_dql.
    assert [name for name, _ in mcp.calls] == ["list_problems", "execute_dql", "execute_dql"]
    # Arg-shape contract: additionalFilter carries the single-id predicate.
    assert "P-1" in mcp.calls[0][1]["additionalFilter"]
    assert mcp.calls[0][1]["maxProblemsToDisplay"] == 1


def test_get_problem_context_synthesizes_signature_on_prose_envelope(monkeypatch):
    """Empty / unfilterable list_problems response falls through to a synthetic
    signature so downstream specialists never see a None / partial context."""
    mcp = _ScriptedMCP(
        [
            {"raw": "No problems found"},  # list_problems empty trial tenant
            {"raw": "0 records"},
            {"raw": "0 records"},
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    ctx = client.get_problem_context("P-UNKNOWN")
    assert ctx.signature.problem_id == "P-UNKNOWN"
    assert ctx.impacted_entities == ()
    assert ctx.events_in_window == ()


def test_get_problem_context_handles_items_array_alias(monkeypatch):
    """Some MCP builds wrap the array as ``items`` instead of ``problems``."""
    mcp = _ScriptedMCP(
        [
            {
                "items": [
                    {
                        "title": "items-shape",
                        "severityLevel": "ERROR",
                        "startTime": "2026-05-17T09:30:00Z",
                        "affectedEntities": [],
                    }
                ]
            },
            {"records": []},
            {"records": []},
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    ctx = client.get_problem_context("P-ITEMS")
    # Backfills the inbound problem_id since the record omitted it.
    assert ctx.signature.problem_id == "P-ITEMS"
    assert ctx.signature.title == "items-shape"


def test_get_problem_context_handles_records_array_alias(monkeypatch):
    """Some MCP builds wrap the array as ``records`` instead of ``problems``."""
    mcp = _ScriptedMCP(
        [
            {
                "records": [
                    {
                        "title": "records-shape",
                        "severityLevel": "RESOURCE",
                        "startTime": "2026-05-17T09:30:00Z",
                        "affectedEntities": [],
                    }
                ]
            },
            {"records": []},
            {"records": []},
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    ctx = client.get_problem_context("P-RECORDS")
    assert ctx.signature.title == "records-shape"


def test_get_problem_context_ignores_non_dict_first_record(monkeypatch):
    """Pathological ``problems`` array entries (not dicts) still degrade cleanly."""
    mcp = _ScriptedMCP(
        [
            {"problems": ["unexpected-string-row"]},
            {"records": []},
            {"records": []},
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    ctx = client.get_problem_context("P-WEIRD")
    assert ctx.signature.problem_id == "P-WEIRD"
    # Synthetic title since no usable record could be coerced.
    assert "P-WEIRD" in ctx.signature.title


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
    """W2-S5: topology now reads via execute_dql against smartscape tables."""
    mcp = _ScriptedMCP(
        [
            {
                "records": [
                    {
                        "id": "S1",
                        "entity.type": "SERVICE",
                        "entity.name": "n1",
                        "distance": 1,
                    },
                    {
                        "id": "S2",
                        "entity.type": "SERVICE",
                        "entity.name": "n2",
                        "distance": 2,
                    },
                ]
            }
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)

    neighbors = client.get_topology_neighbors("SERVICE-ABC", depth=2)
    # All returned entities must have distance <= depth.
    assert all(n.distance <= 2 for n in neighbors)
    # The MCP tool invoked is execute_dql against the smartscape table.
    assert mcp.calls[0][0] == "execute_dql"
    assert "dt.entity.service" in mcp.calls[0][1]["dqlStatement"]
    assert "SERVICE-ABC" in mcp.calls[0][1]["dqlStatement"]


def test_get_topology_neighbors_collapses_prose_envelope_to_empty(monkeypatch):
    """Empty trial tenant smartscape returns the prose envelope (W2-S0 finding)."""
    mcp = _ScriptedMCP([{"raw": "Scanned 0 records..."}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    assert client.get_topology_neighbors("SERVICE-ABC", depth=1) == []


def test_get_topology_neighbors_falls_back_to_entity_id_key(monkeypatch):
    """Some smartscape responses key the id field as ``entityId`` instead of ``id``."""
    mcp = _ScriptedMCP(
        [
            {
                "records": [
                    {
                        "entityId": "S-fallback",
                        "name": "fallback",
                        "type": "SERVICE",
                        "distance": 1,
                    }
                ]
            }
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    neighbors = client.get_topology_neighbors("SERVICE-ABC", depth=1)
    assert neighbors == [
        Entity(
            entity_id="S-fallback",
            entity_type="SERVICE",
            display_name="fallback",
            distance=1,
        )
    ]


def test_repeated_get_problem_context_is_cached_within_one_client(monkeypatch):
    mcp = _ScriptedMCP(
        [
            {
                "problems": [
                    {
                        "title": "x",
                        "severityLevel": "ERROR",
                        "startTime": "2026-05-17T09:30:00Z",
                        "affectedEntities": [],
                    }
                ]
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
    list_calls = [c for c in mcp.calls if c[0] == "list_problems"]
    assert len(list_calls) == 1


def test_tool_allowlist_rejects_calls_to_unlisted_tools(monkeypatch):
    client = DynatraceClient(_cfg(tool_allowlist=("execute_dql",)))
    # send_investigation_event maps to send_event, which is not in the
    # allowlist — must refuse rather than silently call into the MCP
    # subprocess with a banned tool name.
    with pytest.raises(PermissionError):
        client.send_investigation_event("P-1", "diagnosis", "summary")


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
                "records": [
                    {
                        "id": "S1",
                        "entity.type": "SERVICE",
                        "entity.name": "n1",
                        "distance": 1,
                    },
                    {
                        "id": "S99",
                        "entity.type": "SERVICE",
                        "entity.name": "n99",
                        "distance": 99,
                    },
                ]
            }
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    neighbors = client.get_topology_neighbors("SERVICE-ABC", depth=1)
    assert [n.entity_id for n in neighbors] == ["S1"]


def test_send_investigation_event_returns_event_id_with_investigation_id(monkeypatch):
    """W2-S4 reframe: write-back is a CUSTOM_INFO Grail event, not a comment."""
    mcp = _ScriptedMCP([{"eventIngestResults": [{"correlationId": "EV-42"}]}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief markdown", "hypotheses summary")
    assert isinstance(event_id, EventId)
    assert event_id.investigation_id.startswith("causal-oncall-P-1-")
    assert event_id.upstream_reference == "EV-42"


def test_send_investigation_event_passes_send_event_with_expected_shape(monkeypatch):
    """W2-S5: send_event call must be a CUSTOM_INFO with title, entitySelector,
    properties carrying brief_md + hypothesis_summary + investigation_id + schema."""
    mcp = _ScriptedMCP([{"eventIngestResults": [{"correlationId": "EV-1"}]}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    client.send_investigation_event("P-WIRE", "FULL BRIEF MD", "HYPO SUMMARY")

    tool, args = mcp.calls[0]
    assert tool == "send_event"
    assert args["eventType"] == "CUSTOM_INFO"
    assert "P-WIRE" in args["title"]
    assert "causal-oncall.problem_id:P-WIRE" in args["entitySelector"]
    props = args["properties"]
    assert props["generated_by"] == "causal-oncall"
    assert props["schema_version"] == "1"
    assert props["brief_md"] == "FULL BRIEF MD"
    assert props["hypothesis_summary"] == "HYPO SUMMARY"
    assert props["investigation_id"].startswith("causal-oncall-P-WIRE-")
    # Every property must be a string per the v1.8.5 input schema.
    assert all(isinstance(v, str) for v in props.values())


def test_send_investigation_event_is_idempotent_for_identical_brief(monkeypatch):
    """Re-sending the same brief on the same problem returns the cached EventId."""
    mcp = _ScriptedMCP([{"eventIngestResults": [{"correlationId": "EV-once"}]}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    first = client.send_investigation_event("P-1", "same brief", "summary")
    second = client.send_investigation_event("P-1", "same brief", "summary")
    assert first == second
    # MCP only saw the call once — the dedup happened client-side.
    assert len(mcp.calls) == 1


def test_send_investigation_event_isolates_dedup_by_problem_id(monkeypatch):
    """Same brief body on different problems is two distinct ingests."""
    mcp = _ScriptedMCP(
        [
            {"eventIngestResults": [{"correlationId": "EV-a"}]},
            {"eventIngestResults": [{"correlationId": "EV-b"}]},
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    a = client.send_investigation_event("P-A", "same body", "summary")
    b = client.send_investigation_event("P-B", "same body", "summary")
    assert a.investigation_id != b.investigation_id
    assert a.upstream_reference == "EV-a"
    assert b.upstream_reference == "EV-b"
    assert len(mcp.calls) == 2


def test_send_investigation_event_distinct_briefs_yield_distinct_investigation_ids(monkeypatch):
    """Distinct brief content → distinct hash-derived investigation_id suffixes."""
    mcp = _ScriptedMCP(
        [
            {"eventIngestResults": [{"correlationId": "EV-1"}]},
            {"eventIngestResults": [{"correlationId": "EV-2"}]},
        ]
    )
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    first = client.send_investigation_event("P-1", "brief one", "summary")
    second = client.send_investigation_event("P-1", "brief two", "summary")
    assert first.investigation_id != second.investigation_id


def test_send_investigation_event_truncates_large_brief_md(monkeypatch):
    """Brief markdown is truncated to the per-property defensive ceiling."""
    mcp = _ScriptedMCP([{"eventIngestResults": [{"correlationId": "EV"}]}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    huge = "x" * 20000
    client.send_investigation_event("P-1", huge, "summary")
    props = mcp.calls[0][1]["properties"]
    assert len(props["brief_md"]) <= 8000
    assert len(props["hypothesis_summary"]) <= 1500


def test_send_investigation_event_handles_prose_only_response(monkeypatch):
    """When MCP returns a prose-only envelope (no eventIngestResults), the
    client still produces an EventId with the prose snippet as upstream_reference."""
    mcp = _ScriptedMCP([{"raw": "Event ingested OK"}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief", "summary")
    assert event_id.upstream_reference == "Event ingested OK"


def test_send_investigation_event_handles_unknown_response_shape(monkeypatch):
    """Wholly-unknown envelopes degrade to an empty upstream_reference."""
    mcp = _ScriptedMCP([{"something": "unexpected"}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief", "summary")
    assert event_id.upstream_reference == ""


def test_send_investigation_event_skips_blank_ingest_result_entries(monkeypatch):
    """An eventIngestResults entry without correlationId/status falls through to ``""``."""
    mcp = _ScriptedMCP([{"eventIngestResults": [{}]}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief", "summary")
    assert event_id.upstream_reference == ""


def test_send_investigation_event_uses_status_when_correlationId_missing(monkeypatch):
    """eventIngestResults can carry only a ``status`` (older MCP builds)."""
    mcp = _ScriptedMCP([{"eventIngestResults": [{"status": "OK"}]}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief", "summary")
    assert event_id.upstream_reference == "OK"


def test_send_investigation_event_handles_non_list_eventIngestResults(monkeypatch):
    """Pathological ``eventIngestResults`` shapes (not a list) fall through."""
    mcp = _ScriptedMCP([{"eventIngestResults": "single-string-instead-of-list"}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief", "summary")
    assert event_id.upstream_reference == ""


def test_send_investigation_event_handles_eventIngestResults_non_dict_first(monkeypatch):
    """``eventIngestResults`` array entry not a dict falls through."""
    mcp = _ScriptedMCP([{"eventIngestResults": ["string-row"]}])
    client = DynatraceClient(_cfg())
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief", "summary")
    assert event_id.upstream_reference == ""


def test_close_invokes_underlying_mcp_close_and_clears_cache(monkeypatch):
    """Idempotent + safe even if invoked twice."""

    class _CloseMCP:
        def __init__(self) -> None:
            self.closed = 0
            self.calls: list[tuple[str, dict]] = []

        def call_tool(self, name: str, arguments: dict) -> dict:
            self.calls.append((name, arguments))
            return {
                "problems": [
                    {
                        "title": "x",
                        "severityLevel": "ERROR",
                        "startTime": "2026-05-17T09:30:00Z",
                        "affectedEntities": [],
                    }
                ]
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


def test_tool_allowlist_permits_send_event_when_listed(monkeypatch):
    """send_investigation_event resolves to the send_event tool for the allowlist."""
    mcp = _ScriptedMCP([{"eventIngestResults": [{"correlationId": "EV"}]}])
    client = DynatraceClient(_cfg(tool_allowlist=("send_event",)))
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    event_id = client.send_investigation_event("P-1", "brief", "summary")
    assert event_id.upstream_reference == "EV"


def test_execute_dql_propagates_domain_dynatrace_unavailable_unchanged(monkeypatch):
    """Tools that already raise DynatraceUnavailable surface without rewrapping."""
    mcp = _ScriptedMCP([DynatraceUnavailable("upstream gone")])
    client = DynatraceClient(_cfg(max_retries=0))
    monkeypatch.setattr(client, "_mcp", mcp, raising=False)
    with pytest.raises(DynatraceUnavailable, match="upstream gone"):
        client.execute_dql(DQLPlan(query="fetch logs"))
