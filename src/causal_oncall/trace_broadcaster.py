"""TraceBroadcaster — in-memory event bus for the live trace UI.

Hides: per-problem subscriber queues, queue cleanup on consumer
disconnect, SSE-frame formatting, and the autoincrementing event id
the SSE protocol uses for replay-after-reconnect.

The public surface is intentionally tiny:

* ``publish(problem_id, event)`` — synchronous fire-and-forget. Safe to
  call from the orchestrator's hot path. Also appends the event to a
  bounded per-problem replay buffer so a subscriber that attaches *after*
  the events were published still receives the full sequence.
* ``async subscribe(problem_id)`` — replays any buffered events first,
  then yields ``TraceEvent`` per live published event. If the buffer
  already ends in a terminal event (``brief-ready`` /
  ``memory-short-circuit``) the iterator completes after replay so the
  HTTP stream ends and the browser EventSource closes without
  reconnecting. Caller may also break out of the loop to disconnect.
* ``subscriber_count(problem_id)`` — observability into live subscribers
  for healthchecks and for cleaning up zombie investigations.
* ``close(problem_id)`` — terminate all subscribers cleanly (e.g. after
  brief-ready event has been delivered).

The replay buffer solves the demo-mode SSE race: in demo mode the
orchestrator runs synchronously inside the webhook POST and emits every
TraceEvent *before* the browser's EventSource finishes connecting. With a
per-problem ring buffer a late subscriber still gets the full sequence
ending in the terminal event, then the stream completes. The buffer is
bounded (ring of the last N events) and evicted by an LRU cap on the
number of tracked problems so it never leaks for the demo's lifetime.

The broadcaster is single-process: it lives in app.state and dies with
the uvicorn worker. For Cloud Run multi-instance scale we'd swap the
backend for Pub/Sub or Redis; that's out of W2 scope per PLAN §"Out of
scope".
"""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict, deque
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

#: Terminal kinds: once one is buffered for a problem_id, the
#: investigation is complete. A subscriber that replays a buffer ending in
#: one of these gets the full sequence and then the stream closes (no
#: reconnect). ``brief-ready`` is the canonical terminator; a
#: memory-short-circuit run still ends with a ``brief-ready``, so only
#: ``brief-ready`` finalises the stream.
_TERMINAL_KINDS: frozenset[TraceEventKind] = frozenset({"brief-ready"})

#: Ring-buffer depth per problem_id. A full investigation emits ~12
#: events; 64 leaves generous headroom for replans without unbounded
#: growth.
_DEFAULT_BUFFER_SIZE = 64

#: LRU cap on the number of problem_ids whose buffers we retain. Demo
#: scale is a handful of concurrent investigations; 256 is far above that
#: and bounds memory even if a crawler hammers the stream endpoint.
_DEFAULT_MAX_PROBLEMS = 256


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

    def __init__(
        self,
        *,
        buffer_size: int = _DEFAULT_BUFFER_SIZE,
        max_problems: int = _DEFAULT_MAX_PROBLEMS,
    ) -> None:
        self._subscribers: dict[str, list[asyncio.Queue[TraceEvent | None]]] = {}
        # Per-problem replay ring buffer. OrderedDict gives us LRU eviction
        # (move_to_end on touch, popitem(last=False) to evict the oldest).
        self._buffers: OrderedDict[str, deque[TraceEvent]] = OrderedDict()
        # Problem ids whose buffer ends in a terminal event. A subscriber
        # that attaches to a completed problem replays the buffer and then
        # the stream closes — it never blocks waiting for live events that
        # will never come, so the browser EventSource does not reconnect.
        self._completed: set[str] = set()
        self._buffer_size = buffer_size
        self._max_problems = max_problems

    def publish(self, problem_id: str, event: TraceEvent) -> None:
        if self._on_publish_hook is not None:
            self._on_publish_hook(problem_id, event)
        self._buffer_event(problem_id, event)
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
        # Snapshot the buffered events and whether the problem already
        # completed BEFORE registering as a live subscriber. This closes
        # the race: events that were published while the orchestrator ran
        # synchronously (demo mode) are replayed here, and any that arrive
        # between the snapshot and registration are delivered live via the
        # queue. Registering first then snapshotting could double-deliver
        # the boundary event; snapshotting first then registering cannot
        # drop one because the buffer append and queue fan-out happen
        # together under the single-threaded event loop.
        buffered = list(self._buffers.get(problem_id, ()))
        already_complete = problem_id in self._completed
        for event in buffered:
            yield event
        if already_complete:
            # Replay ended in a terminal event; the investigation is done.
            # Close the stream so the client stops here and does not
            # reconnect waiting for events that will never arrive.
            return

        queue: asyncio.Queue[TraceEvent | None] = asyncio.Queue()
        self._subscribers.setdefault(problem_id, []).append(queue)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    # Sentinel from close() — terminate the iterator cleanly.
                    return
                yield event
                if event.kind in _TERMINAL_KINDS:
                    # The terminal event has been delivered live; end the
                    # stream so the response completes and the browser
                    # EventSource closes instead of idling until timeout.
                    return
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

    def _buffer_event(self, problem_id: str, event: TraceEvent) -> None:
        """Append to the problem's ring buffer with LRU eviction.

        Retains events even when no subscriber is attached so a late
        subscriber can replay them. Bounds memory two ways: a per-problem
        ring (oldest events drop once the ring is full) and an LRU cap on
        the number of tracked problems.
        """
        buffer = self._buffers.get(problem_id)
        if buffer is None:
            buffer = deque(maxlen=self._buffer_size)
            self._buffers[problem_id] = buffer
        else:
            # Touch for LRU recency.
            self._buffers.move_to_end(problem_id)
        buffer.append(event)
        if event.kind in _TERMINAL_KINDS:
            self._completed.add(problem_id)
        # Evict the least-recently-used problem buffers beyond the cap.
        while len(self._buffers) > self._max_problems:
            evicted_id, _ = self._buffers.popitem(last=False)
            self._completed.discard(evicted_id)

    def _remove_subscriber(self, problem_id: str, queue: asyncio.Queue[TraceEvent | None]) -> None:
        queues = self._subscribers.get(problem_id, [])
        if queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(problem_id, None)
