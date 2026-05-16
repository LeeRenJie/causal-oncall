"""TDD spec for Evidence.

Confidence must be in [0, 1]; summary + hypothesis_key non-empty.
These are construction-time invariants the synthesizer relies on
when it sorts evidence by confidence.
"""

from __future__ import annotations

import pytest

from causal_oncall.domain.evidence import Evidence


def _kwargs(**overrides):
    base = dict(
        specialist="triage",
        kind="log_pattern",
        summary="5xx burst on /charge",
        stance="supports",
        hypothesis_key="db_pool_exhaustion",
        confidence=0.7,
    )
    base.update(overrides)
    return base


def test_construction_succeeds_on_valid_fields():
    ev = Evidence(**_kwargs())
    assert ev.specialist == "triage"
    assert ev.confidence == 0.7


def test_confidence_above_one_is_rejected():
    with pytest.raises(ValueError):
        Evidence(**_kwargs(confidence=1.4))


def test_confidence_below_zero_is_rejected():
    with pytest.raises(ValueError):
        Evidence(**_kwargs(confidence=-0.01))


def test_empty_summary_is_rejected():
    with pytest.raises(ValueError):
        Evidence(**_kwargs(summary=""))


def test_empty_hypothesis_key_is_rejected():
    with pytest.raises(ValueError):
        Evidence(**_kwargs(hypothesis_key=""))


def test_evidence_is_frozen_and_hashable():
    a = Evidence(**_kwargs())
    b = Evidence(**_kwargs())
    assert a == b
    {a, b}  # noqa: B018 — exercise hashability across set membership
