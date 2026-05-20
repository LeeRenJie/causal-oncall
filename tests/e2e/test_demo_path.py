"""End-to-end demo-path replay.

This test is the safety net for the 3-minute demo. It loads the canonical
``payment_latency_spike.json`` fixture, replays it through the full agent
(boundaries faked), and asserts the resulting brief matches the wow-moment
contract:
  * top-ranked hypothesis is the seeded one,
  * top recommendation is actionable (non-empty, non-default),
  * a memory short-circuit fires when the seeded 10-resolved fixture
    contains a matching incident.

If this fails, the demo will fail. Treat it as P0.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.mark.skip(
    reason="W3-S2 (memory short-circuit) lands the full e2e wiring; W1 covers via integration test."
)
def test_demo_path_payment_latency_spike_yields_expected_top_hypothesis():
    """Wow #1: 90-second brief with the seeded hypothesis on top."""
    json.loads((FIXTURES / "incidents" / "payment_latency_spike.json").read_text(encoding="utf-8"))
    raise NotImplementedError(
        "Wire orchestrator + specialists with fakes seeded from the fixture, "
        "run orch.handle(payload), and assert the top-ranked hypothesis matches "
        "the fixture's expected_top_hypothesis_key."
    )


@pytest.mark.skip(reason="W3-S2 introduces memory pre-flight; not in W1 scope.")
def test_demo_path_memory_short_circuit_when_prior_resolved_exists():
    """Wow #3: pre-flight match short-circuits to a 30-second brief."""
    json.loads((FIXTURES / "incidents" / "payment_latency_spike.json").read_text(encoding="utf-8"))
    json.loads((FIXTURES / "memory_seeds" / "seed_10_resolved.json").read_text(encoding="utf-8"))
    raise NotImplementedError(
        "Seed the memory store from seed_10_resolved.json (which includes a "
        "matching prior), run handle(payload), and assert "
        "brief.memory_short_circuit is True."
    )


@pytest.mark.skip(
    reason="W2-S3 wires Slack-driven rejection; W1 only covers the unit-level replan."
)
def test_demo_path_replan_after_rejection_changes_top_hypothesis():
    """Wow #2: hypothesis rejection → visible replan."""
    raise NotImplementedError(
        "Run a full investigation, call reject_hypothesis_and_replan() on the "
        "top hypothesis, and assert the replanned brief has a different top key."
    )
