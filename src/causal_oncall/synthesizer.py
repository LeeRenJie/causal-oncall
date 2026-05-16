"""Synthesizer — turns a bag of Evidence into a ranked Brief.

Hides: the Gemini prompt construction, the hypothesis-ranking algorithm
(weighted by stance + confidence + specialist trust), structured-output
schema validation, Markdown rendering, and Dynatrace deep-link generation.

The synthesizer is the only module in the codebase that talks to an LLM
for prose generation; every other LLM call is a specialist deciding
*what to look at next*, not generating user-facing text.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from causal_oncall.domain.brief import Brief
from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.problem_signature import ProblemSignature


@dataclass(frozen=True, slots=True)
class SynthesizerConfig:
    """Knobs that affect ranking + LLM choice."""

    gemini_model_id: str
    dynatrace_base_url: str  # used to build deep links
    max_hypotheses: int = 5
    min_supporting_confidence: float = 0.3


class Synthesizer:
    """Compose evidences into a brief with a ranked causal hypothesis tree."""

    def __init__(self, config: SynthesizerConfig) -> None:
        self._config = config

    def compose(
        self,
        signature: ProblemSignature,
        evidences: Iterable[Evidence],
        *,
        memory_short_circuit: bool = False,
    ) -> Brief:
        """Rank candidate hypotheses, draft prose, return a finalized Brief.

        Ranking is deterministic given the input evidences: same inputs in,
        same Brief out. The LLM call is responsible for the prose only;
        the ranks and supporting-evidence groupings come from a pure
        function the LLM cannot override. This keeps the brief replayable
        from cached LLM responses for the demo.

        Raises:
            SynthesisFailed: if the LLM call returns an unparseable result
                or no hypothesis clears ``min_supporting_confidence``.
        """
        raise NotImplementedError(
            "Group evidences by hypothesis_key, rank by composite score, draft "
            "prose via Gemini, and return a finalized Brief."
        )
