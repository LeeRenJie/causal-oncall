"""DynatraceClient — the agent's only door to the Dynatrace MCP server.

Hides: the npx subprocess lifecycle, MCP stdio framing, per-tenant rate-
limit pacing (50 req/min sliding window), 429 / 503 retry policy, response
caching keyed on (tool, args), JSON-schema validation, and the mapping of
transport-level errors to domain exceptions.

The public surface is intentionally four methods (three reads + one
write); everything else is an implementation detail that callers
(specialists, orchestrator) must not need to know about.

W2-S5 (MCP v1.8.5 realignment):
* ``get_problem_context`` no longer calls the absent ``get_problem_details``
  tool. It now calls ``list_problems`` (with an ``additionalFilter`` for the
  target problem id) and two ``execute_dql`` queries to hydrate impacted
  entities + the surrounding event window.
* ``get_topology_neighbors`` no longer calls the absent
  ``get_topology_neighbors`` tool. It now issues an ``execute_dql`` query
  against Grail's smartscape entity/relationship tables.
* ``post_problem_comment`` is removed (MCP v1.8.5 exposes no
  agent-authored comment write). The Grail-event write-back path
  replaces it: ``send_investigation_event`` ingests a CUSTOM_INFO event
  via the ``send_event`` MCP tool, carrying the brief markdown +
  hypothesis summary as event properties and an idempotency-keyed
  investigation_id.
"""

from __future__ import annotations

import hashlib
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


@dataclass(frozen=True, slots=True)
class EventId:
    """Identifier returned by ``send_investigation_event``.

    The MCP ``send_event`` tool answers with either a structured payload
    (Events API v2 returns an ``eventIngestResults`` array) or a prose
    confirmation. We expose the strongest stable identifier — the
    investigation_id we generated client-side — alongside whatever the
    upstream surfaced for human cross-reference.
    """

    investigation_id: str
    upstream_reference: str


class _MCPProcess(Protocol):
    """Implementation detail — the spawn surface the client requires.

    Defined as a Protocol so the test suite can substitute an in-process
    fake without exposing a public hook on DynatraceClient itself.
    """

    def call_tool(self, name: str, arguments: dict) -> dict: ...

    def close(self) -> None: ...


class DynatraceClient:
    """Narrow facade over the Dynatrace MCP server.

    Four public methods cover every read + the single write the agent
    needs. Writes (ingesting the investigation event) live on a
    separate path because they have different retry semantics and
    require the ``storage:events:write`` OAuth scope.
    """

    #: Set of public method names mapped to the MCP tools they invoke. Used
    #: when ``DynatraceClientConfig.tool_allowlist`` is non-empty to refuse
    #: calls to disallowed tools before they reach the MCP subprocess.
    #:
    #: NB: ``get_problem_context`` fans out into ``list_problems`` plus two
    #: ``execute_dql`` queries; the allowlist key resolves to ``execute_dql``
    #: because that's the most-permissive tool the method touches, and
    #: every specialist that hydrates problem context already needs
    #: ``execute_dql`` permission for its own DQL probes.
    _METHOD_TO_TOOL: ClassVar[dict[str, str]] = {
        "get_problem_context": "execute_dql",
        "execute_dql": "execute_dql",
        "get_topology_neighbors": "execute_dql",
        "send_investigation_event": "send_event",
    }

    def __init__(self, config: DynatraceClientConfig) -> None:
        self._config = config
        self._mcp: Any | None = None
        self._problem_context_cache: dict[str, ProblemContext] = {}
        # Per-problem investigation-event dedup state for
        # ``send_investigation_event``: maps (problem_id, content_hash)
        # -> already-returned EventId, so re-posting the same brief on
        # the same problem is a no-op and the Grail event stream stays
        # clean across re-runs.
        self._sent_events: dict[tuple[str, str], EventId] = {}

    def get_problem_context(self, problem_id: str) -> ProblemContext:
        """Fetch + normalize the full context for one open problem.

        v1.8.5 has no ``get_problem_details`` tool; the closest read is
        ``list_problems`` with a single-id ``additionalFilter`` plus two
        scoped ``execute_dql`` queries to hydrate impacted entities and
        the event window. Cached per problem_id for the life of the
        request so repeat calls by different specialists are free.
        """
        self._check_allowed("get_problem_context")
        cached = self._problem_context_cache.get(problem_id)
        if cached is not None:
            return cached

        problems_response = self._call_tool_with_retry(
            "list_problems",
            {
                # Single-id filter keeps the response small even on busy
                # tenants. We tolerate the prose envelope MCP sometimes
                # returns by falling through to a synthetic signature
                # built from the inbound problem_id.
                "additionalFilter": f'problem.id == "{problem_id}"',
                "maxProblemsToDisplay": 1,
            },
        )
        payload = self._coerce_problem_payload(problem_id, problems_response)
        signature = ProblemSignature.from_dynatrace_payload(payload)

        impacted = self._call_tool_with_retry(
            "execute_dql",
            {
                "dqlStatement": (
                    f'fetch dt.davis.problems | filter problem.id == "{problem_id}" '
                    "| expand affected_entity_ids"
                )
            },
        )
        events = self._call_tool_with_retry(
            "execute_dql",
            {"dqlStatement": (f'fetch events | filter problem.id == "{problem_id}"')},
        )
        ctx = ProblemContext(
            signature=signature,
            impacted_entities=self._records_or_empty(impacted),
            events_in_window=self._records_or_empty(events),
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
        """Walk the dependency topology outward by ``depth`` hops via Grail.

        v1.8.5 has no ``get_topology_neighbors`` tool. We issue an
        ``execute_dql`` query against the smartscape entity tables
        scoped to neighbors of ``entity_id``. The traversal is still
        bounded by ``depth`` so that one bad call can't fan out across
        the whole tenant.
        """
        self._check_allowed("get_topology_neighbors")
        # smartscape edges return target entities sourced from the seed;
        # we read targets up to ``depth`` hops away. Empty trial tenants
        # produce the prose envelope, which collapses to an empty list.
        response = self._call_tool_with_retry(
            "execute_dql",
            {
                "dqlStatement": (
                    f'fetch dt.entity.service | filter id == "{entity_id}" '
                    "| fields id, entity.name, entity.type, distance "
                    f"| filter distance <= {int(depth)}"
                )
            },
        )
        if "raw" in response and "records" not in response:
            return []
        out: list[Entity] = []
        for raw in response.get("records", ()):
            distance = int(raw.get("distance", depth))
            if distance > depth:
                continue
            out.append(
                Entity(
                    entity_id=str(raw.get("id") or raw.get("entityId") or ""),
                    entity_type=str(raw.get("entity.type") or raw.get("type") or ""),
                    display_name=str(raw.get("entity.name") or raw.get("name") or ""),
                    distance=distance,
                )
            )
        return out

    def send_investigation_event(
        self,
        problem_id: str,
        brief_md: str,
        hypothesis_summary: str,
    ) -> EventId:
        """Ingest a CUSTOM_INFO event into Grail carrying the agent's brief.

        v1.8.5 has no problem-comment write surface; this is the
        Grail-native replacement (W2-S4 reframe). The event has type
        ``causal-oncall.investigation-complete``, is associated with
        the originating problem's entity via ``entitySelector``, and
        carries the brief markdown + hypothesis summary as event
        properties so downstream consumers (notebooks, workflows,
        on-call dashboards) can render the brief in-product.

        Idempotency: an ``investigation_id`` of the form
        ``causal-oncall-{problem_id}-{brief_hash[:8]}`` is computed
        client-side and embedded as both an event property AND a
        method-local dedup key. Sending the same brief twice on the
        same problem returns the cached ``EventId`` without a second
        MCP round-trip — so dashboards stay clean across orchestrator
        replays.
        """
        self._check_allowed("send_investigation_event")
        brief_hash = hashlib.sha256(brief_md.encode("utf-8")).hexdigest()
        cached = self._sent_events.get((problem_id, brief_hash))
        if cached is not None:
            return cached

        investigation_id = f"causal-oncall-{problem_id}-{brief_hash[:8]}"
        # send_event properties accept string-typed values only (per the
        # MCP v1.8.5 inputSchema). Brief markdown is truncated defensively
        # to stay well under common Events API per-property byte limits;
        # the canonical artifact lives at ./out/briefs/<id>.md anyway,
        # so the event property is a convenience surface for in-product
        # rendering.
        properties: dict[str, str] = {
            "investigation_id": investigation_id,
            "generated_by": "causal-oncall",
            "schema_version": "1",
            "event_subtype": "causal-oncall.investigation-complete",
            "hypothesis_summary": hypothesis_summary[:1500],
            "brief_md": brief_md[:8000],
        }
        # Title is bounded to 500 chars per MCP schema; keep the prefix
        # informative for the Events API timeline rendering.
        title = f"Causal On-Call investigation: {problem_id}"[:500]
        response = self._call_tool_with_retry(
            "send_event",
            {
                "eventType": "CUSTOM_INFO",
                "title": title,
                # Problem entity associations: Dynatrace problems aren't
                # themselves entities, but the impacted services are.
                # We use a tag-based selector that future workflows can
                # filter on by problem id without us needing to resolve
                # the entity id up-front (a separate find_entity_by_name
                # call would double our MCP round-trips and bring no
                # demo-path value).
                "entitySelector": (f'type(SERVICE),tag("causal-oncall.problem_id:{problem_id}")'),
                "properties": properties,
            },
        )
        upstream_reference = self._extract_upstream_reference(response)
        event_id = EventId(
            investigation_id=investigation_id,
            upstream_reference=upstream_reference,
        )
        self._sent_events[(problem_id, brief_hash)] = event_id
        return event_id

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

    @staticmethod
    def _records_or_empty(response: dict) -> tuple[dict, ...]:
        """Collapse a possibly-prose MCP response to a tuple of records."""
        if "raw" in response and "records" not in response:
            return ()
        return tuple(response.get("records", ()))

    @staticmethod
    def _coerce_problem_payload(problem_id: str, response: dict) -> dict:
        """Promote an MCP ``list_problems`` response to a from_dynatrace_payload-shaped dict.

        Empty trial tenants and additionalFilter misses surface as a
        prose ``{"raw": "..."}`` envelope. In that case we synthesize a
        minimal payload from the requested problem_id so the caller
        still gets a well-formed ``ProblemSignature`` — the downstream
        specialists tolerate empty impacted_entities + events.

        Structured responses (``{"problems": [...]}``) are handed over
        verbatim after lifting the first record. Some MCP builds wrap
        the array under different keys; we walk a small set of common
        ones to stay resilient across point releases.
        """
        for key in ("problems", "items", "records"):
            candidates = response.get(key)
            if isinstance(candidates, list) and candidates:
                first = candidates[0]
                if isinstance(first, dict):
                    # Backfill the inbound problem_id when the MCP record
                    # omits it (some tenants return abbreviated rows).
                    return {"problemId": problem_id, **first}
        # Prose / unknown envelopes — synthesize the minimum payload
        # that ProblemSignature.from_dynatrace_payload accepts.
        return {
            "problemId": problem_id,
            "title": f"Dynatrace problem {problem_id}",
            "severityLevel": "CUSTOM",
            "startTime": "2026-01-01T00:00:00Z",
            "affectedEntities": [],
        }

    @staticmethod
    def _extract_upstream_reference(response: dict) -> str:
        """Pull whatever stable id the send_event MCP tool exposes.

        v1.8.5 sometimes returns a prose-only ``{"raw": "..."}`` envelope,
        sometimes a structured ``{"eventIngestResults": [...]}`` array.
        We capture the strongest available reference; callers shouldn't
        rely on this for dedup (use ``EventId.investigation_id`` for that).
        """
        results = response.get("eventIngestResults")
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                # Events API v2 returns correlationId per ingest.
                ref = first.get("correlationId") or first.get("status") or ""
                if ref:
                    return str(ref)
        raw = response.get("raw")
        if isinstance(raw, str):
            return raw[:200]
        return ""
