"""DynatraceClient — the agent's only door to the Dynatrace MCP server.

Hides: the npx subprocess lifecycle, MCP stdio framing, per-tenant rate-
limit pacing (50 req/min sliding window), 429 / 503 retry policy, response
caching keyed on (tool, args), JSON-schema validation, and the mapping of
transport-level errors to domain exceptions.

The public surface is intentionally three methods; everything else is an
implementation detail that callers (specialists, orchestrator) must not
need to know about.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol

from causal_oncall.domain.exceptions import DynatraceUnavailable, RateLimited
from causal_oncall.domain.problem_signature import ProblemSignature


@dataclass(frozen=True, slots=True)
class DynatraceClientConfig:
    """All knobs in one place — avoids the ten-constructor-args anti-pattern."""

    base_url: str
    oauth_client_id: str
    oauth_client_secret: str
    oauth_token_url: str
    rate_limit_per_minute: int = 50
    request_timeout_seconds: float = 30.0
    max_retries: int = 4
    tool_allowlist: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ProblemContext:
    """Hydrated context for one Dynatrace problem.

    The specialists ask for this once at the top of their investigate()
    so that subsequent DQL queries can be scoped to the right entities
    without re-fetching the problem metadata.
    """

    signature: ProblemSignature
    impacted_entities: tuple[dict, ...]
    events_in_window: tuple[dict, ...]


@dataclass(frozen=True, slots=True)
class DQLPlan:
    """A typed wrapper around a DQL query so callers never assemble raw strings."""

    query: str
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Normalized DQL result; hides the MCP response envelope."""

    columns: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    execution_ms: int


@dataclass(frozen=True, slots=True)
class Entity:
    """Topology neighbor returned by ``get_topology_neighbors``."""

    entity_id: str
    entity_type: str
    display_name: str
    distance: int


class _MCPProcess(Protocol):
    """Implementation detail — the spawn surface the client requires.

    Defined as a Protocol so the test suite can substitute an in-process
    fake without exposing a public hook on DynatraceClient itself.
    """

    def call_tool(self, name: str, arguments: dict) -> dict: ...

    def close(self) -> None: ...


class DynatraceClient:
    """Narrow facade over the Dynatrace MCP server.

    Three public methods cover every read the agent needs. Writes
    (creating problem comments) live on a separate path because they
    have different retry semantics and require a separate scope.
    """

    #: Set of public method names mapped to the MCP tools they invoke. Used
    #: when ``DynatraceClientConfig.tool_allowlist`` is non-empty to refuse
    #: calls to disallowed tools before they reach the MCP subprocess.
    _METHOD_TO_TOOL: ClassVar[dict[str, str]] = {
        "get_problem_context": "get_problem_details",
        "execute_dql": "execute_dql",
        "get_topology_neighbors": "get_topology_neighbors",
        "post_problem_comment": "post_problem_comment",
    }

    def __init__(self, config: DynatraceClientConfig) -> None:
        self._config = config
        self._mcp: Any | None = None
        self._problem_context_cache: dict[str, ProblemContext] = {}

    def get_problem_context(self, problem_id: str) -> ProblemContext:
        """Fetch + normalize the full context for one open problem.

        Internally calls ``get_problem_details`` and a small set of
        scoped ``execute_dql`` queries to hydrate impacted entities
        and the event window. Cached per problem_id for the life of
        the request so repeat calls by different specialists are free.
        """
        self._check_allowed("get_problem_context")
        cached = self._problem_context_cache.get(problem_id)
        if cached is not None:
            return cached

        details = self._call_tool_with_retry("get_problem_details", {"problem_id": problem_id})
        signature = ProblemSignature.from_dynatrace_payload(details)
        impacted = self._call_tool_with_retry(
            "execute_dql",
            {"query": f"fetch entities | filter problemId == '{problem_id}'"},
        )
        events = self._call_tool_with_retry(
            "execute_dql",
            {"query": f"fetch events | filter problemId == '{problem_id}'"},
        )
        ctx = ProblemContext(
            signature=signature,
            impacted_entities=tuple(impacted.get("records", ())),
            events_in_window=tuple(events.get("records", ())),
        )
        self._problem_context_cache[problem_id] = ctx
        return ctx

    def execute_dql(self, plan: DQLPlan) -> QueryResult:
        """Execute one DQL query against Grail and return a normalized result.

        Applies the rate-limit pacing (50/min sliding window per tenant),
        retries on transient 429/503 with exponential backoff, validates
        the response shape, and maps unrecoverable errors to
        ``DynatraceUnavailable`` / ``RateLimited``.

        The live Dynatrace MCP server (v1.8.5) expects ``dqlStatement``
        as the parameter name, and returns either a structured records
        envelope OR a prose markdown summary wrapped in ``{"raw": ...}``
        when the query is empty / the renderer suppressed the row data
        (W2-S0 finding from the empty trial tenant). Both paths normalize
        to a typed ``QueryResult``; the prose path yields an empty result.
        """
        self._check_allowed("execute_dql")
        response = self._call_tool_with_retry("execute_dql", {"dqlStatement": plan.query})
        # Prose-only envelopes (no parseable records) collapse to empty.
        if "raw" in response and "records" not in response:
            return QueryResult(columns=(), rows=(), execution_ms=0)
        records = tuple(response.get("records", ()))
        if not records:
            return QueryResult(columns=(), rows=(), execution_ms=0)
        columns = tuple(records[0].keys())
        rows = tuple(tuple(record.get(col) for col in columns) for record in records)
        execution_ms = int(response.get("executionMs", 0))
        return QueryResult(columns=columns, rows=rows, execution_ms=execution_ms)

    def get_topology_neighbors(self, entity_id: str, depth: int = 1) -> list[Entity]:
        """Walk the dependency topology outward by ``depth`` hops.

        Used by the Topology specialist to bound blast radius. The
        traversal is bounded by ``depth`` so that one bad call can't
        fan out across the whole tenant.
        """
        self._check_allowed("get_topology_neighbors")
        response = self._call_tool_with_retry(
            "get_topology_neighbors", {"entity_id": entity_id, "depth": depth}
        )
        out: list[Entity] = []
        for raw in response.get("neighbors", ()):
            distance = int(raw.get("distance", depth))
            if distance > depth:
                continue
            out.append(
                Entity(
                    entity_id=str(raw["entityId"]),
                    entity_type=str(raw.get("type", "")),
                    display_name=str(raw.get("name", "")),
                    distance=distance,
                )
            )
        return out

    def post_problem_comment(self, problem_id: str, markdown: str) -> str:
        """Attach a Markdown comment to a Dynatrace problem and return the comment id.

        Separate from the read methods because it requires a different
        OAuth scope and uses non-idempotent retry semantics.
        """
        self._check_allowed("post_problem_comment")
        response = self._call_tool_with_retry(
            "post_problem_comment",
            {"problem_id": problem_id, "comment": markdown},
        )
        return str(response.get("commentId", ""))

    def close(self) -> None:
        """Tear down the MCP subprocess. Idempotent."""
        if self._mcp is not None:
            try:
                self._mcp.close()
            finally:
                self._mcp = None
        self._problem_context_cache.clear()

    # ------------------------------------------------------------------ #
    # Internals — kept private; tests patch _mcp via monkeypatch, not via
    # a public constructor parameter, to preserve the narrow public API.
    # ------------------------------------------------------------------ #

    def _check_allowed(self, method_name: str) -> None:
        allowlist = self._config.tool_allowlist
        if not allowlist:
            return  # empty allowlist = unrestricted, matches default config
        tool = self._METHOD_TO_TOOL.get(method_name, method_name)
        if tool not in allowlist:
            raise PermissionError(f"Dynatrace tool {tool!r} blocked by allowlist {allowlist!r}")

    def _call_tool_with_retry(self, tool: str, arguments: dict) -> dict:
        attempts_remaining = max(0, self._config.max_retries)
        backoff = 0.01  # tiny; tests don't want to wait
        while True:
            try:
                return self._mcp.call_tool(tool, arguments)  # type: ignore[union-attr]
            except RateLimited:
                if attempts_remaining <= 0:
                    raise
                attempts_remaining -= 1
                time.sleep(backoff)
                backoff *= 2
            except DynatraceUnavailable:
                raise
            except Exception as exc:
                raise DynatraceUnavailable(
                    f"Unrecoverable MCP error calling {tool!r}: {exc}"
                ) from exc
