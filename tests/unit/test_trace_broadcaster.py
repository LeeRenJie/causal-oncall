"""TDD spec for TraceBroadcaster — the SSE event bus for the live trace UI.

Decouples the orchestrator from the HTTP layer: orchestrator publishes
lifecycle events (specialist-dispatched, specialist-completed, …) into
the broadcaster; the SSE handler subscribes and streams them to one
browser tab. Multiple subscribers per problem_id are supported (so a
judge and a tester can both watch the same investigation).
"""

from __future__ import annotations

import asyncio

import pytest

from causal_oncall.trace_broadcaster import TraceBroadcaster, TraceEvent


def test_publish_without_subscribers_is_a_noop():
    bus = TraceBroadcaster()
    # No subscribers — no error, no blocking.
    bus.publish("P-1", TraceEvent(kind="orchestrator-started", data={"hello": "world"}))


async def _wait_for_subscriber(bus: TraceBroadcaster, problem_id: str) -> None:
    for _ in range(100):
        if bus.subscriber_count(problem_id) > 0:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"no subscriber appeared for {problem_id!r}")


async def test_subscribe_receives_events_published_after_subscription():
    bus = TraceBroadcaster()

    async def collect_two() -> list[TraceEvent]:
        out: list[TraceEvent] = []
        async for ev in bus.subscribe("P-1"):
            out.append(ev)
            if len(out) == 2:
                break
        return out

    consumer = asyncio.create_task(collect_two())
    await _wait_for_subscriber(bus, "P-1")
    bus.publish("P-1", TraceEvent(kind="specialist-dispatched", data={"name": "triage"}))
    bus.publish("P-1", TraceEvent(kind="specialist-completed", data={"name": "triage"}))
    got = await asyncio.wait_for(consumer, timeout=1.0)
    assert [e.kind for e in got] == ["specialist-dispatched", "specialist-completed"]


async def test_subscribers_for_different_problem_ids_are_isolated():
    bus = TraceBroadcaster()

    async def first_event(problem_id: str) -> TraceEvent:
        async for ev in bus.subscribe(problem_id):
            return ev
        raise AssertionError("subscription closed without yielding")

    t1 = asyncio.create_task(first_event("P-1"))
    t2 = asyncio.create_task(first_event("P-2"))
    await _wait_for_subscriber(bus, "P-1")
    await _wait_for_subscriber(bus, "P-2")
    bus.publish("P-2", TraceEvent(kind="orchestrator-started", data={}))
    # P-1 still has no event; we resolve only when P-1 also gets one.
    bus.publish("P-1", TraceEvent(kind="orchestrator-started", data={"sentinel": "ok"}))
    got1 = await asyncio.wait_for(t1, timeout=1.0)
    got2 = await asyncio.wait_for(t2, timeout=1.0)
    assert got1.data["sentinel"] == "ok"
    assert got2.kind == "orchestrator-started"


async def test_subscriber_cleanup_on_consumer_disconnect():
    """When a subscriber stops iterating, the broadcaster drops its queue."""
    bus = TraceBroadcaster()

    async def consume_then_disconnect() -> None:
        async for _ev in bus.subscribe("P-1"):
            break  # disconnect after first event

    consumer = asyncio.create_task(consume_then_disconnect())
    # Wait until the subscriber is registered before publishing so the
    # event can't race ahead of the subscription.
    for _ in range(100):
        if bus.subscriber_count("P-1") > 0:
            break
        await asyncio.sleep(0)
    bus.publish("P-1", TraceEvent(kind="orchestrator-started", data={}))
    await asyncio.wait_for(consumer, timeout=1.0)
    # Yield one tick so the generator's `finally` block runs.
    await asyncio.sleep(0)
    # After disconnect the broadcaster has zero subscribers for P-1.
    assert bus.subscriber_count("P-1") == 0


def test_publish_emits_brief_ready_event_with_payload():
    bus = TraceBroadcaster()
    captured: list[TraceEvent] = []

    # Synchronous subscriber via a low-level hook for unit-test introspection.
    def on_publish(_problem_id: str, ev: TraceEvent) -> None:
        captured.append(ev)

    bus._on_publish_hook = on_publish
    bus.publish("P-1", TraceEvent(kind="brief-ready", data={"top": "db_pool_exhaustion"}))
    assert len(captured) == 1
    assert captured[0].kind == "brief-ready"
    assert captured[0].data["top"] == "db_pool_exhaustion"


def test_trace_event_to_sse_renders_an_id_event_data_block():
    ev = TraceEvent(kind="specialist-dispatched", data={"name": "topology"})
    sse_text = ev.to_sse(event_id=42)
    # SSE protocol: id:, event:, data:, blank line terminator.
    assert "id: 42" in sse_text
    assert "event: specialist-dispatched" in sse_text
    # JSON data block is compact (no spaces) so payload byte count stays small.
    assert '"name":"topology"' in sse_text
    assert sse_text.endswith("\n\n")


async def test_subscribe_closes_cleanly_when_broadcaster_closed():
    bus = TraceBroadcaster()

    async def consume() -> list[TraceEvent]:
        out: list[TraceEvent] = []
        async for ev in bus.subscribe("P-1"):
            out.append(ev)
        return out

    consumer = asyncio.create_task(consume())
    await _wait_for_subscriber(bus, "P-1")
    bus.publish("P-1", TraceEvent(kind="orchestrator-started", data={}))
    await asyncio.sleep(0)
    bus.close("P-1")
    got = await asyncio.wait_for(consumer, timeout=1.0)
    assert len(got) == 1


@pytest.mark.parametrize(
    "kind", ["orchestrator-started", "specialist-dispatched", "specialist-completed", "brief-ready"]
)
def test_trace_event_kinds_round_trip_through_sse(kind):
    ev = TraceEvent(kind=kind, data={"k": "v"})
    text = ev.to_sse(event_id=1)
    assert f"event: {kind}" in text


def test_remove_subscriber_tolerates_already_detached_queue():
    """Defensive: removing a queue that was already removed is a no-op."""
    bus = TraceBroadcaster()
    queue: asyncio.Queue = asyncio.Queue()
    # Directly call the internal — simulates a duplicate teardown path
    # (e.g. close() and consumer-disconnect racing).
    bus._remove_subscriber("P-1", queue)  # nothing registered — must not raise
    # Register, drop, re-drop — second drop is a no-op against an empty list.
    bus._subscribers["P-1"] = [queue]
    bus._remove_subscriber("P-1", queue)
    assert bus.subscriber_count("P-1") == 0
    bus._remove_subscriber("P-1", queue)
    assert bus.subscriber_count("P-1") == 0


def test_remove_subscriber_keeps_problem_when_other_subscribers_remain():
    """When two clients watch the same trace, dropping one leaves the other intact."""
    bus = TraceBroadcaster()
    q1: asyncio.Queue = asyncio.Queue()
    q2: asyncio.Queue = asyncio.Queue()
    bus._subscribers["P-1"] = [q1, q2]
    bus._remove_subscriber("P-1", q1)
    # The other subscriber survives — problem_id stays in the map.
    assert bus.subscriber_count("P-1") == 1
    assert "P-1" in bus._subscribers
