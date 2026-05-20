"""FakeGeminiClient — deterministic stand-in for the Vertex AI Gemini client.

The Curator calls into a tiny narrow Gemini-shaped surface (one method:
``synthesize_pattern(prompt: str) -> dict``). Tests inject this fake so
no unit test ever hits Vertex AI. The fake records every call for
assertion + returns a canned response that mimics the shape the real
Gemini Pro returns under the Curator's structured-output prompt.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ClassVar


class FakeGeminiClient:
    """Records calls + returns canned synthesis responses.

    Attributes:
        calls: list of every ``synthesize_pattern`` prompt the Curator has
            issued, in order. Tests assert on length + content.
        responses: queue of canned dicts; the next ``synthesize_pattern``
            call pops the next response. Falls back to ``default_response``
            once the queue empties so tests don't have to enumerate every
            cluster explicitly.
        default_response: the response returned when ``responses`` is empty.
        prompt_input_tokens / prompt_output_tokens: per-call token counts
            advertised back to the Curator's cost-estimation seam. Real
            Gemini returns these on the response object; the fake stubs
            them so the CuratorReport cost field is deterministic.
        fail_with: when set, the next call raises this exception instead
            of returning a response. Cleared after firing once.
    """

    _DEFAULT_RESPONSE: ClassVar[dict[str, Any]] = {
        "pattern_summary": "Recurring incident pattern.",
        "recommended_action": "Investigate using the bundled few-shot examples.",
        "diagnostic_dql": "fetch logs | filter dt.entity.service in [...]",
    }

    def __init__(
        self,
        *,
        responses: Iterable[dict[str, Any]] | None = None,
        default_response: dict[str, Any] | None = None,
        prompt_input_tokens: int = 1200,
        prompt_output_tokens: int = 400,
    ) -> None:
        self.calls: list[str] = []
        self.responses: list[dict[str, Any]] = list(responses) if responses else []
        self.default_response: dict[str, Any] = (
            default_response if default_response is not None else dict(self._DEFAULT_RESPONSE)
        )
        self.prompt_input_tokens = prompt_input_tokens
        self.prompt_output_tokens = prompt_output_tokens
        self.fail_with: Exception | None = None

    def synthesize_pattern(self, prompt: str) -> dict[str, Any]:
        """Return the next canned response, recording the prompt.

        Mirrors the Curator's narrow Gemini-shaped seam: prompt in,
        structured dict out. The real client wraps Vertex AI's
        ``generate_content`` + JSON parsing; this fake skips both.
        """
        self.calls.append(prompt)
        if self.fail_with is not None:
            exc, self.fail_with = self.fail_with, None
            raise exc
        if self.responses:
            return dict(self.responses.pop(0))
        return dict(self.default_response)

    # The Curator multiplies prompt_input_tokens / prompt_output_tokens by
    # the published per-token rates; exposing them as attributes keeps the
    # cost computation deterministic without leaking tokenizer details.
    def token_counts(self) -> tuple[int, int]:
        """Return ``(input_tokens, output_tokens)`` advertised for the last call."""
        return (self.prompt_input_tokens, self.prompt_output_tokens)
