"""TDD spec for SlackNotifier.

Slack is faked at the WebClient seam; the test pins the brief→block-kit
contract and the idempotency story without depending on slack-sdk.
"""

from __future__ import annotations

from causal_oncall.slack_notifier import (
    FeedbackEvent,
    MessageRef,
    SlackNotifier,
    SlackNotifierConfig,
)
from tests.conftest import make_brief


def _cfg() -> SlackNotifierConfig:
    return SlackNotifierConfig(
        bot_token="xoxb-test",
        brief_channel_id="C123",
        signing_secret="secret",
    )


class _FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict] = []
        self.next_ts = 1000.0

    def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs)
        self.next_ts += 1
        return {"ok": True, "channel": kwargs["channel"], "ts": f"{self.next_ts:.6f}"}


def test_post_brief_returns_a_message_ref(monkeypatch):
    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)

    ref = notifier.post_brief(make_brief(problem_id="P-1"), feedback_channel="C123")
    assert isinstance(ref, MessageRef)
    assert ref.channel_id == "C123"
    assert ref.message_ts != ""


def test_post_brief_is_idempotent_on_problem_id(monkeypatch):
    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)

    brief = make_brief(problem_id="P-1")
    ref1 = notifier.post_brief(brief, feedback_channel="C123")
    ref2 = notifier.post_brief(brief, feedback_channel="C123")
    assert ref1 == ref2
    assert len(fake.posted) == 1


def test_post_brief_includes_top_recommendation_in_block_kit(monkeypatch):
    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)

    brief = make_brief(problem_id="P-1")
    notifier.post_brief(brief, feedback_channel="C123")
    payload = fake.posted[0]
    # block-kit lives under "blocks"; the top recommendation should be
    # serialized somewhere within them so a Slack reader sees it without
    # expanding the brief.
    rendered = repr(payload.get("blocks", []))
    assert brief.top_recommendation in rendered


def test_await_feedback_returns_none_on_timeout(monkeypatch):
    notifier = SlackNotifier(_cfg())
    monkeypatch.setattr(notifier, "_poll_for_feedback", lambda ref, t: None, raising=False)
    ref = MessageRef(channel_id="C123", message_ts="1.0")
    assert notifier.await_feedback(ref, timeout_seconds=0.01) is None


def test_await_feedback_returns_feedback_event_when_user_clicks(monkeypatch):
    notifier = SlackNotifier(_cfg())
    fb = FeedbackEvent(
        message_ref=MessageRef(channel_id="C123", message_ts="1.0"),
        top_hypothesis_correct=True,
        confirmed_root_cause_key="db_pool_exhaustion",
        confirmed_fix="Increased pool size",
    )
    monkeypatch.setattr(notifier, "_poll_for_feedback", lambda ref, t: fb, raising=False)
    assert notifier.await_feedback(fb.message_ref, timeout_seconds=1.0) is fb


def test_post_brief_renders_memory_short_circuit_badge(monkeypatch):
    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)
    brief = make_brief(problem_id="P-X", memory_short_circuit=True)
    notifier.post_brief(brief, feedback_channel="C123")
    rendered = repr(fake.posted[0].get("blocks", []))
    assert "seen this incident shape" in rendered
