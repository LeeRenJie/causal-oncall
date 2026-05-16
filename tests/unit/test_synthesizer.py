"""TDD spec for Synthesizer.

Ranking is deterministic — the LLM provides prose only; tests pin
ranking on real Evidence shapes without invoking Gemini.
"""

from __future__ import annotations

from causal_oncall.domain.exceptions import SynthesisFailed
from causal_oncall.synthesizer import Synthesizer, SynthesizerConfig
from tests.conftest import make_evidence, make_signature

import pytest


def _cfg(**overrides) -> SynthesizerConfig:
    base = dict(
        gemini_model_id="gemini-3-pro-preview",
        dynatrace_base_url="https://abc.live.dynatrace.com",
        max_hypotheses=5,
        min_supporting_confidence=0.3,
    )
    base.update(overrides)
    return SynthesizerConfig(**base)


class _StubGemini:
    """Stubs out the LLM. Returns canned prose for the call the synthesizer makes."""

    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls = 0

    def __call__(self, prompt: str) -> dict:
        self.calls += 1
        return self.response


def _install(synth: Synthesizer, gem: _StubGemini, monkeypatch):
    monkeypatch.setattr(synth, "_llm_call", gem, raising=False)


def test_synthesizer_ranks_hypotheses_by_supporting_evidence_count(monkeypatch):
    synth = Synthesizer(_cfg())
    _install(
        synth,
        _StubGemini(
            {
                "hypotheses": {
                    "db_pool_exhaustion": {
                        "title": "DB pool exhausted",
                        "next_action": "Roll back v412.",
                    },
                    "noisy_neighbor": {
                        "title": "Noisy neighbor CPU steal",
                        "next_action": "Migrate to dedicated host.",
                    },
                }
            }
        ),
        monkeypatch,
    )
    evidences = [
        make_evidence(hypothesis_key="db_pool_exhaustion", confidence=0.8),
        make_evidence(hypothesis_key="db_pool_exhaustion", confidence=0.7),
        make_evidence(hypothesis_key="db_pool_exhaustion", confidence=0.7),
        make_evidence(hypothesis_key="noisy_neighbor", confidence=0.6),
    ]
    brief = synth.compose(make_signature(), evidences)
    assert brief.ranked_hypotheses[0].key == "db_pool_exhaustion"
    assert brief.ranked_hypotheses[0].rank == 1


def test_synthesizer_caps_at_max_hypotheses(monkeypatch):
    synth = Synthesizer(_cfg(max_hypotheses=2))
    _install(
        synth,
        _StubGemini(
            {
                "hypotheses": {
                    f"h{i}": {"title": f"H{i}", "next_action": "..."} for i in range(5)
                }
            }
        ),
        monkeypatch,
    )
    evidences = [
        make_evidence(hypothesis_key=f"h{i}", confidence=0.7) for i in range(5)
    ]
    brief = synth.compose(make_signature(), evidences)
    assert len(brief.ranked_hypotheses) == 2


def test_synthesizer_drops_hypotheses_below_min_supporting_confidence(monkeypatch):
    synth = Synthesizer(_cfg(min_supporting_confidence=0.5))
    _install(
        synth,
        _StubGemini(
            {
                "hypotheses": {
                    "strong": {"title": "Strong", "next_action": "..."},
                    "weak": {"title": "Weak", "next_action": "..."},
                }
            }
        ),
        monkeypatch,
    )
    evidences = [
        make_evidence(hypothesis_key="strong", confidence=0.9),
        make_evidence(hypothesis_key="weak", confidence=0.2),
    ]
    brief = synth.compose(make_signature(), evidences)
    keys = {h.key for h in brief.ranked_hypotheses}
    assert "weak" not in keys


def test_synthesizer_raises_synthesis_failed_on_unparseable_llm_output(monkeypatch):
    synth = Synthesizer(_cfg())
    _install(synth, _StubGemini({"unexpected": "shape"}), monkeypatch)
    with pytest.raises(SynthesisFailed):
        synth.compose(make_signature(), [make_evidence(confidence=0.9)])


def test_synthesizer_marks_memory_short_circuit_briefs(monkeypatch):
    synth = Synthesizer(_cfg())
    _install(
        synth,
        _StubGemini(
            {"hypotheses": {"x": {"title": "X", "next_action": "..."}}}
        ),
        monkeypatch,
    )
    brief = synth.compose(
        make_signature(),
        [make_evidence(hypothesis_key="x", confidence=0.9)],
        memory_short_circuit=True,
    )
    assert brief.memory_short_circuit is True
