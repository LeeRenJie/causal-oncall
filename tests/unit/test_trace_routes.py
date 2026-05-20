"""TDD spec for the SSE handler + HTML-rendering helpers (W2-S2).

The actual FastAPI routes live in ``app.py`` (uncovered glue). The
coverable behavior — converting a TraceBroadcaster subscription into an
SSE byte stream, and rendering the single-page trace UI — is owned by
this module so it can be unit-tested without standing up the HTTP stack.
"""

from __future__ import annotations

import asyncio

import pytest

from causal_oncall.trace_broadcaster import TraceBroadcaster, TraceEvent
from causal_oncall.trace_routes import render_trace_page, stream_sse_for_problem


def test_render_trace_page_includes_the_problem_id_and_sse_endpoint():
    html = render_trace_page("P-42")
    assert "P-42" in html
    assert "/webhook/dynatrace-problem/stream/P-42" in html
    # EventSource is the SSE client; presence proves the page is wired up.
    assert "EventSource" in html
    # No external scripts — page must work offline behind a corporate proxy.
    assert "http://" not in html or 'src="http' not in html


def test_render_trace_page_escapes_problem_id_to_prevent_html_injection():
    html = render_trace_page("<script>alert(1)</script>")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


async def test_stream_sse_for_problem_yields_each_event_as_an_sse_frame():
    bus = TraceBroadcaster()

    async def collect_three_frames() -> list[bytes]:
        out: list[bytes] = []
        async for frame in stream_sse_for_problem(bus, "P-1"):
            out.append(frame)
            if len(out) == 3:
                break
        return out

    consumer = asyncio.create_task(collect_three_frames())
    # Wait until the subscription registers.
    for _ in range(100):
        if bus.subscriber_count("P-1") > 0:
            break
        await asyncio.sleep(0)
    bus.publish("P-1", TraceEvent(kind="orchestrator-started", data={}))
    bus.publish("P-1", TraceEvent(kind="specialist-dispatched", data={"name": "triage"}))
    bus.publish("P-1", TraceEvent(kind="brief-ready", data={"top_hypothesis_key": "x"}))

    frames = await asyncio.wait_for(consumer, timeout=1.0)
    assert len(frames) == 3
    assert all(isinstance(f, bytes) for f in frames)
    # SSE frame ids are monotonic and incrementing.
    assert b"id: 1\n" in frames[0]
    assert b"id: 2\n" in frames[1]
    assert b"id: 3\n" in frames[2]
    # Event kinds are echoed by the SSE handler.
    assert b"event: orchestrator-started" in frames[0]
    assert b"event: brief-ready" in frames[2]


async def test_stream_sse_for_problem_emits_initial_retry_directive():
    """SSE clients use the retry directive for reconnect backoff."""
    bus = TraceBroadcaster()

    async def first_frame() -> bytes:
        async for frame in stream_sse_for_problem(bus, "P-1"):
            return frame
        raise AssertionError("stream closed without yielding")

    consumer = asyncio.create_task(first_frame())
    for _ in range(100):
        if bus.subscriber_count("P-1") > 0:
            break
        await asyncio.sleep(0)
    bus.publish("P-1", TraceEvent(kind="orchestrator-started", data={}))
    frame = await asyncio.wait_for(consumer, timeout=1.0)
    # First frame should include both the retry hint and the first event.
    assert b"retry: " in frame
    assert b"event: orchestrator-started" in frame


async def test_stream_sse_for_problem_terminates_when_broadcaster_closes():
    bus = TraceBroadcaster()

    async def collect_all() -> list[bytes]:
        out: list[bytes] = []
        async for frame in stream_sse_for_problem(bus, "P-1"):
            out.append(frame)
        return out

    consumer = asyncio.create_task(collect_all())
    for _ in range(100):
        if bus.subscriber_count("P-1") > 0:
            break
        await asyncio.sleep(0)
    bus.publish("P-1", TraceEvent(kind="orchestrator-started", data={}))
    await asyncio.sleep(0)
    bus.close("P-1")
    out = await asyncio.wait_for(consumer, timeout=1.0)
    # We get one frame for the event, then clean termination (no infinite hang).
    assert len(out) == 1


async def test_stream_sse_cleans_up_subscriber_on_consumer_cancel():
    """If the HTTP client disconnects mid-stream, the broadcaster sees zero subscribers."""
    bus = TraceBroadcaster()

    async def consume_forever() -> None:
        async for _frame in stream_sse_for_problem(bus, "P-1"):
            pass  # never break — relies on cancellation

    consumer = asyncio.create_task(consume_forever())
    for _ in range(100):
        if bus.subscriber_count("P-1") > 0:
            break
        await asyncio.sleep(0)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    await asyncio.sleep(0)
    assert bus.subscriber_count("P-1") == 0
