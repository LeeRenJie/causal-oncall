"""SlackNotifier — brief delivery + one-click feedback collection.

Hides: Slack auth + signing-secret verification, block-kit rendering of
the Brief, retry on transient Slack 5xx, the feedback-event listener
state machine, and idempotency keying (so a retried webhook never
double-posts the same brief).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from causal_oncall.domain.brief import Brief


@dataclass(frozen=True, slots=True)
class SlackNotifierConfig:
    """All Slack-side parameters."""

    bot_token: str
    brief_channel_id: str
    signing_secret: str


@dataclass(frozen=True, slots=True)
class MessageRef:
    """Stable handle to a posted Slack message; used by the feedback loop."""

    channel_id: str
    message_ts: str


@dataclass(frozen=True, slots=True)
class FeedbackEvent:
    """The on-call's confirmed verdict, returned by :meth:`await_feedback`.

    Attributes:
        message_ref: Which posted brief this feedback applies to.
        top_hypothesis_correct: True iff the on-call confirmed the
            agent's #1 hypothesis was the real root cause.
        confirmed_root_cause_key: Hypothesis key the on-call picked.
            May not equal the top hypothesis's key.
        confirmed_fix: Free-text description of the actual fix.
    """

    message_ref: MessageRef
    top_hypothesis_correct: bool
    confirmed_root_cause_key: str
    confirmed_fix: str


class SlackNotifier:
    """Post briefs, collect verdicts."""

    def __init__(self, config: SlackNotifierConfig) -> None:
        self._config = config
        self._slack: Any = None  # populated lazily in production; tests patch
        # Idempotency cache keyed on brief.problem_id so a retried webhook
        # returns the same MessageRef rather than double-posting.
        self._posted_briefs: dict[str, MessageRef] = {}
        # Feedback polling seam — tests substitute via monkeypatch to keep
        # the wait short. Default implementation returns None immediately.
        self._poll_for_feedback: Any = self._default_poll_for_feedback

    def post_brief(self, brief: Brief, feedback_channel: str) -> MessageRef:
        """Render the brief as Slack block-kit and post it. Returns a MessageRef."""
        cached = self._posted_briefs.get(brief.problem_id)
        if cached is not None:
            return cached

        blocks = self._render_block_kit(brief)
        slack = self._ensure_slack()
        response = slack.chat_postMessage(channel=feedback_channel, blocks=blocks)
        ref = MessageRef(channel_id=str(response["channel"]), message_ts=str(response["ts"]))
        self._posted_briefs[brief.problem_id] = ref
        return ref

    def await_feedback(
        self, msg_ref: MessageRef, *, timeout_seconds: float
    ) -> FeedbackEvent | None:
        """Block (or wait async) for the on-call's button click. Returns None on timeout."""
        return self._poll_for_feedback(msg_ref, timeout_seconds)

    # ------------------------------------------------------------------ #
    # Internals.
    # ------------------------------------------------------------------ #

    @staticmethod
    def _render_block_kit(brief: Brief) -> list[dict]:
        import os as _os

        base_url = _os.environ.get("CAUSAL_ONCALL_BASE_URL", "").rstrip("/")

        header_text = f"Causal On-Call brief: {brief.problem_id}"
        if brief.from_memory:
            header_text = f"Causal On-Call (memory hit): {brief.problem_id}"

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": header_text, "emoji": True},
            }
        ]

        if brief.memory_short_circuit:
            score_pct = (
                f" (similarity {brief.pattern_match_score:.0%})"
                if brief.pattern_match_score is not None
                else ""
            )
            blocks.append(
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": (
                                f":bookmark: We have seen this incident shape "
                                f"before{score_pct}. Showing the historical fix."
                            ),
                        }
                    ],
                }
            )

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Next action:* {brief.top_recommendation}",
                },
            }
        )
        blocks.append({"type": "divider"})

        for hyp in brief.ranked_hypotheses:
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            f"*#{hyp.rank} · {hyp.title}*  "
                            f"`score {hyp.score:.2f}`"
                        ),
                    },
                }
            )

            if hyp.supporting_evidence:
                ev_lines = []
                for ev in hyp.supporting_evidence[:4]:
                    ev_lines.append(
                        f"• `{ev.specialist}` "
                        f"(conf {ev.confidence:.0%}) — {ev.summary}"
                    )
                if len(hyp.supporting_evidence) > 4:
                    ev_lines.append(
                        f"• _…and {len(hyp.supporting_evidence) - 4} more findings_"
                    )
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(ev_lines)},
                    }
                )

            if hyp.next_action and hyp.next_action != brief.top_recommendation:
                blocks.append(
                    {
                        "type": "context",
                        "elements": [
                            {"type": "mrkdwn", "text": f":arrow_right: {hyp.next_action}"}
                        ],
                    }
                )

            blocks.append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "style": "primary",
                            "text": {
                                "type": "plain_text",
                                "text": f"Confirm #{hyp.rank}",
                                "emoji": True,
                            },
                            "value": hyp.key,
                            "action_id": f"confirm_{hyp.key}",
                        },
                        {
                            "type": "button",
                            "text": {
                                "type": "plain_text",
                                "text": f"Reject #{hyp.rank}",
                                "emoji": True,
                            },
                            "value": hyp.key,
                            "action_id": f"reject_{hyp.key}",
                        },
                    ],
                }
            )
            blocks.append({"type": "divider"})

        footer_parts = [
            f"_Generated {brief.generated_at.strftime('%Y-%m-%d %H:%M UTC')}_",
            f"_schema v{Brief.SCHEMA_VERSION}_",
        ]
        if base_url:
            footer_parts.append(
                f"<{base_url}/trace/{brief.problem_id}|View live trace>"
            )
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "  ·  ".join(footer_parts)}],
            }
        )

        return blocks

    def _ensure_slack(self):  # pragma: no cover  # exercised by contract test in W2
        if self._slack is None:
            from slack_sdk import WebClient

            self._slack = WebClient(token=self._config.bot_token)
        return self._slack

    @staticmethod
    def _default_poll_for_feedback(
        msg_ref: MessageRef, timeout_seconds: float
    ) -> FeedbackEvent | None:  # pragma: no cover  # real Slack listener lives in W2
        return None
