"""SlackNotifier — brief delivery + one-click feedback collection.

Hides: Slack auth + signing-secret verification, block-kit rendering of
the Brief, retry on transient Slack 5xx, the feedback-event listener
state machine, and idempotency keying (so a retried webhook never
double-posts the same brief).
"""

from __future__ import annotations

from dataclasses import dataclass

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

    def post_brief(self, brief: Brief, feedback_channel: str) -> MessageRef:
        """Render the brief as Slack block-kit and post it. Returns a MessageRef.

        Idempotent on ``brief.problem_id``: a retry won't post twice.
        """
        raise NotImplementedError(
            "Render the brief into Slack block-kit, post it to feedback_channel, "
            "and return a MessageRef. Honor brief.problem_id for idempotency."
        )

    def await_feedback(self, msg_ref: MessageRef, *, timeout_seconds: float) -> FeedbackEvent | None:
        """Block (or wait async) for the on-call's button click. Returns None on timeout.

        The verdict captured here flows into ``MemoryStore.update_resolution``
        and ``PhoenixTracer.record_outcome``.
        """
        raise NotImplementedError(
            "Wait up to timeout_seconds for the on-call's button click on the "
            "posted brief and return the FeedbackEvent (or None on timeout)."
        )
