"""Synthesizer — turns a bag of Evidence into a ranked Brief.

Hides: the Gemini prompt construction, the hypothesis-ranking algorithm
(weighted by stance + confidence + specialist trust), structured-output
schema validation, Markdown rendering, and Dynatrace deep-link generation.

The synthesizer is the only module in the codebase that talks to an LLM
for prose generation; every other LLM call is a specialist deciding
*what to look at next*, not generating user-facing text.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from causal_oncall.domain.brief import Brief, Hypothesis
from causal_oncall.domain.evidence import Evidence
from causal_oncall.domain.exceptions import SynthesisFailed
from causal_oncall.domain.problem_signature import ProblemSignature

# Per PLAN W1-S2 lock: 0.4 * supporting_count + 0.4 * mean_confidence +
# 0.2 * specialist_trust. Normalize supporting_count by max across hypotheses
# so the score stays in [0, 1].
_TRUST_BY_SPECIALIST: dict[str, float] = {
    "triage": 0.85,
    "deploy_correlation": 0.90,
    "anomaly_window": 0.80,
    "topology": 0.75,
    "vuln_sec": 0.70,
    "memory": 0.95,
}
_DEFAULT_TRUST = 0.6


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
        # Indirection so tests monkeypatch the LLM seam without touching network.
        self._llm_call: Any = self._default_llm_call

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
        evidence_list = list(evidences)
        grouped = self._group_by_hypothesis(evidence_list)

        # Drop hypotheses whose max supporting confidence is below the floor.
        # "supporting" here means stance != refutes.
        kept: dict[str, list[Evidence]] = {}
        for key, items in grouped.items():
            supporting = [e for e in items if e.stance != "refutes"]
            if not supporting:
                continue
            max_conf = max(e.confidence for e in supporting)
            if max_conf < self._config.min_supporting_confidence:
                continue
            kept[key] = items

        if not kept:
            raise SynthesisFailed(
                "No hypothesis cleared min_supporting_confidence "
                f"{self._config.min_supporting_confidence}"
            )

        # Ask the LLM for prose. Tests stub _llm_call to skip the real call.
        prompt = self._build_prompt(signature, kept)
        try:
            llm_response = self._llm_call(prompt)
        except Exception as exc:
            raise SynthesisFailed(f"LLM call failed: {exc}") from exc

        if not isinstance(llm_response, dict) or "hypotheses" not in llm_response:
            raise SynthesisFailed(f"LLM response missing 'hypotheses' key: {llm_response!r}")
        llm_hypotheses = llm_response["hypotheses"]
        if not isinstance(llm_hypotheses, dict):
            raise SynthesisFailed(
                f"LLM 'hypotheses' must be a mapping; got {type(llm_hypotheses).__name__}"
            )

        ranked = self._rank_hypotheses(kept)
        # Apply the LLM prose; cap at max_hypotheses.
        capped = ranked[: self._config.max_hypotheses]
        hypotheses: list[Hypothesis] = []
        for rank, (key, items, score) in enumerate(capped, start=1):
            prose = llm_hypotheses.get(key)
            if not isinstance(prose, dict):
                # Fall back to a minimal placeholder; never raise here because
                # we've already validated the LLM response shape above.
                title = key
                next_action = "Investigate further; LLM did not provide guidance."
            else:
                title = str(prose.get("title", key))
                next_action = str(prose.get("next_action", ""))

            supporting = tuple(
                self._enrich_with_dynatrace_link(e, signature)
                for e in items
                if e.stance == "supports"
            )
            refuting = tuple(
                self._enrich_with_dynatrace_link(e, signature)
                for e in items
                if e.stance == "refutes"
            )
            hypotheses.append(
                Hypothesis(
                    key=key,
                    title=title,
                    rank=rank,
                    score=score,
                    supporting_evidence=supporting,
                    refuting_evidence=refuting,
                    next_action=next_action,
                )
            )

        return Brief(
            problem_id=signature.problem_id,
            generated_at=datetime.now(UTC),
            ranked_hypotheses=tuple(hypotheses),
            top_recommendation=hypotheses[0].next_action,
            memory_short_circuit=memory_short_circuit,
        )

    def _enrich_with_dynatrace_link(
        self, evidence: Evidence, signature: ProblemSignature
    ) -> Evidence:
        """Attach a clickable Dynatrace UI link to the evidence if missing.

        The link points back to the problem detail page in the configured
        Dynatrace tenant so the on-call can jump straight to the underlying
        data. Specialists may pre-supply richer links (e.g. DQL deep links)
        — we never overwrite those.
        """
        if evidence.dynatrace_links:
            return evidence
        link = f"{self._config.dynatrace_base_url}/ui/problems/{signature.problem_id}"
        return Evidence(
            specialist=evidence.specialist,
            kind=evidence.kind,
            summary=evidence.summary,
            stance=evidence.stance,
            hypothesis_key=evidence.hypothesis_key,
            confidence=evidence.confidence,
            dynatrace_links=(link,),
            raw_payload=evidence.raw_payload,
        )

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _group_by_hypothesis(evidences: list[Evidence]) -> dict[str, list[Evidence]]:
        grouped: dict[str, list[Evidence]] = defaultdict(list)
        for ev in evidences:
            grouped[ev.hypothesis_key].append(ev)
        return dict(grouped)

    @staticmethod
    def _rank_hypotheses(
        grouped: dict[str, list[Evidence]],
    ) -> list[tuple[str, list[Evidence], float]]:
        """Apply the locked composite score: 0.4*supp + 0.4*mean_conf + 0.2*trust."""
        max_supporting = (
            max(sum(1 for e in items if e.stance == "supports") for items in grouped.values()) or 1
        )
        out: list[tuple[str, list[Evidence], float]] = []
        for key, items in grouped.items():
            supporting = [e for e in items if e.stance == "supports"]
            supp_norm = len(supporting) / max_supporting
            mean_conf = (
                sum(e.confidence for e in supporting) / len(supporting) if supporting else 0.0
            )
            trust = (
                sum(_TRUST_BY_SPECIALIST.get(e.specialist, _DEFAULT_TRUST) for e in supporting)
                / len(supporting)
                if supporting
                else _DEFAULT_TRUST
            )
            score = 0.4 * supp_norm + 0.4 * mean_conf + 0.2 * trust
            out.append((key, items, score))
        # Sort by score desc; deterministic tie-break on key for replay safety.
        out.sort(key=lambda triple: (-triple[2], triple[0]))
        return out

    def _build_prompt(self, signature: ProblemSignature, grouped: dict[str, list[Evidence]]) -> str:
        lines = [
            "You are the Synthesizer for an SRE pre-mortem agent.",
            f"Problem: {signature.title} (severity={signature.severity}).",
            f"Dynatrace base URL: {self._config.dynatrace_base_url}",
            "Hypotheses with structured evidence follow. For each, produce a",
            "title (<=10 words) and a single-sentence next_action.",
            "Respond ONLY with JSON {hypotheses: {key: {title, next_action}}}.",
        ]
        for key, items in grouped.items():
            lines.append(f"- hypothesis_key={key!r}, evidence_count={len(items)}")
        return "\n".join(lines)

    def _default_llm_call(self, prompt: str) -> dict:  # pragma: no cover  # ADK-runtime-backed
        """Real LLM call — routed through the ADK runtime, not direct genai.

        Production builds the prose step as a Gemini-backed ADK ``LlmAgent``
        and run it via ``Runner`` (see ``adk_runtime.AdkLlmSynthesisCall``),
        which the production wiring assigns to ``self._llm_call``. This
        default is the lazy fall-back when the wiring did not inject the
        ADK call object: it constructs the same ADK-backed call on demand
        so there is *no* direct ``google.genai.generate_content`` path left
        in the synthesis flow. Tests override ``_llm_call`` via monkeypatch
        and never reach this branch.
        """
        from causal_oncall.adk_runtime import AdkLlmSynthesisCall

        call = AdkLlmSynthesisCall(
            model=self._config.gemini_model_id,
            agent_name="causal_oncall_synthesizer",
            instruction=(
                "You are the Synthesizer for Causal On-Call. Given the "
                "problem + grouped evidence, return ONLY a JSON object of the "
                'form {"hypotheses": {key: {title, next_action}}}.'
            ),
        )
        return call(prompt)
