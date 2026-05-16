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

from dataclasses import dataclass, field
from typing import Any, Protocol

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

    def __init__(self, config: DynatraceClientConfig) -> None:
        self._config = config

    def get_problem_context(self, problem_id: str) -> ProblemContext:
        """Fetch + normalize the full context for one open problem.

        Internally calls ``get_problem_details`` and a small set of
        scoped ``execute_dql`` queries to hydrate impacted entities
        and the event window. Cached per problem_id for the life of
        the request so repeat calls by different specialists are free.
        """
        raise NotImplementedError(
            "Fetch problem details via MCP and hydrate impacted entities + "
            "event window into a ProblemContext."
        )

    def execute_dql(self, plan: DQLPlan) -> QueryResult:
        """Execute one DQL query against Grail and return a normalized result.

        Applies the rate-limit pacing (50/min sliding window per tenant),
        retries on transient 429/503 with exponential backoff, validates
        the response shape, and maps unrecoverable errors to
        ``DynatraceUnavailable`` / ``RateLimited``.
        """
        raise NotImplementedError(
            "Execute the DQL plan via MCP, honoring rate-limit pacing + retry, "
            "and return a typed QueryResult."
        )

    def get_topology_neighbors(self, entity_id: str, depth: int = 1) -> list[Entity]:
        """Walk the dependency topology outward by ``depth`` hops.

        Used by the Topology specialist to bound blast radius. The
        traversal is bounded by ``depth`` so that one bad call can't
        fan out across the whole tenant.
        """
        raise NotImplementedError(
            "Traverse the topology graph outward from entity_id by depth hops."
        )

    def post_problem_comment(self, problem_id: str, markdown: str) -> str:
        """Attach a Markdown comment to a Dynatrace problem and return the comment id.

        Separate from the read methods because it requires a different
        OAuth scope and uses non-idempotent retry semantics.
        """
        raise NotImplementedError(
            "Post a Markdown comment to the Dynatrace problem and return its id."
        )

    def close(self) -> None:
        """Tear down the MCP subprocess. Idempotent."""
        raise NotImplementedError(
            "Close the underlying MCP process and release any cached state."
        )
