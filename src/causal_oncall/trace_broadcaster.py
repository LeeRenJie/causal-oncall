"""TraceBroadcaster — in-memory event bus for the live trace UI.

Hides: per-problem subscriber queues, queue cleanup on consumer
disconnect, SSE-frame formatting, and the autoincrementing event id
the SSE protocol uses for replay-after-reconnect.

The public surface is intentionally tiny:

* ``publish(problem_id, event)`` — synchronous fire-and-forget. Safe to
  call from the orchestrator's hot path.
* ``async subscribe(problem_id)`` — yields ``TraceEvent`` per published
  event. Caller breaks out of the loop to disconnect cleanly.
* ``subscriber_count(problem_id)`` — observability into live subscribers
  for healthchecks and for cleaning up zombie investigations.
* ``close(problem_id)`` — terminate all subscribers cleanly (e.g. after
  brief-ready event has been delivered).

The broadcaster is single-process: it lives in app.state and dies with
the uvicorn worker. For Cloud Run multi-instance scale we'd swap the
backend for Pub/Sub or Redis; that's out of W2 scope per PLAN §"Out of
scope".
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

#: SSE event kinds the orchestrator emits. Tight closed set so the UI
#: can dispatch on `event:` rather than parsing data payloads.
TraceEventKind = Literal[
    "orchestrator-started",
    "specialist-dispatched",
    "specialist-completed",
    "synthesizer-started",
    "brief-ready",
    "memory-short-circuit",
    "error",
]


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One agent-lifecycle event suitable for SSE rendering."""

    kind: TraceEventKind
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self, *, event_id: int) -> str:
        """Render as one Server-Sent Events frame (terminator included)."""
        payload = json.dumps(self.data, separators=(",", ":"), sort_keys=True)
        return f"id: {event_id}\nevent: {self.kind}\ndata: {payload}\n\n"


class TraceBroadcaster:
    """In-memory pub/sub for live trace events, keyed on problem_id."""

    #: Optional synchronous hook called for every publish — for unit-test
    #: introspection only. Production code never sets this.
    _on_publish_hook: Callable[[str, TraceEvent], None] | None = None

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[TraceEvent | None]]] = {}

    def publish(self, problem_id: str, event: TraceEvent) -> None:
        if self._on_publish_hook is not None:
            self._on_publish_hook(problem_id, event)
        queues = self._subscribers.get(problem_id)
        if not queues:
            return
        # Fan out to every current subscriber. put_nowait is intentional —
        # if a subscriber is too slow to drain we'd rather drop than block
        # the orchestrator's hot path. Queue is unbounded so put_nowait is
        # effectively non-blocking.
        for queue in queues:
            queue.put_nowait(event)

    async def subscribe(self, problem_id: str) -> AsyncIterator[TraceEvent]:
        queue: asyncio.Queue[TraceEvent | None] = asyncio.Queue()
        self._subscribers.setdefault(problem_id, []).append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Sentinel from close() — terminate the iterator cleanly.
                    return
                yield event
        finally:
            self._remove_subscriber(problem_id, queue)

    def subscriber_count(self, problem_id: str) -> int:
        return len(self._subscribers.get(problem_id, []))

    def close(self, problem_id: str) -> None:
        """Terminate every subscriber on this problem_id with a sentinel."""
        for queue in list(self._subscribers.get(problem_id, [])):
            queue.put_nowait(None)

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    def _remove_subscriber(self, problem_id: str, queue: asyncio.Queue[TraceEvent | None]) -> None:
        queues = self._subscribers.get(problem_id, [])
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(problem_id, None)
