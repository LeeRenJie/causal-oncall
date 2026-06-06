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


def test_post_brief_bypasses_idempotency_cache_in_demo_mode(monkeypatch):
    """In DEMO_MODE every click posts fresh, so a repeated problem_id re-posts.

    The demo reuses fixed problem IDs; the production idempotency cache would
    make a second click a silent no-op, which looks broken on a live demo.
    """
    monkeypatch.setenv("CAUSAL_ONCALL_DEMO_MODE", "true")
    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)

    brief = make_brief(problem_id="P-1")
    ref1 = notifier.post_brief(brief, feedback_channel="C123")
    ref2 = notifier.post_brief(brief, feedback_channel="C123")
    assert len(fake.posted) == 2
    assert ref1.message_ts != ref2.message_ts


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


def test_post_brief_header_says_memory_hit_when_from_memory(monkeypatch):
    from datetime import UTC, datetime

    from causal_oncall.domain.brief import Brief, Hypothesis

    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)
    hyp = Hypothesis(
        key="db_pool_exhaustion",
        title="DB pool exhausted",
        rank=1,
        score=0.91,
        supporting_evidence=(),
        refuting_evidence=(),
        next_action="Roll back",
    )
    brief = Brief(
        problem_id="P-M",
        generated_at=datetime(2026, 5, 17, 9, 31, tzinfo=UTC),
        ranked_hypotheses=(hyp,),
        top_recommendation="Roll back",
        memory_short_circuit=True,
        from_memory=True,
        pattern_match_score=0.92,
    )
    notifier.post_brief(brief, feedback_channel="C123")
    rendered = repr(fake.posted[0].get("blocks", []))
    assert "memory hit" in rendered
    assert "92%" in rendered  # similarity rendered


def test_post_brief_renders_supporting_evidence_under_each_hypothesis(monkeypatch):
    from tests.conftest import make_brief as _mb
    from tests.conftest import make_evidence, make_hypothesis

    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)
    evs = tuple(
        make_evidence(
            specialist=s,
            summary=f"finding from {s}",
            confidence=0.6 + 0.05 * i,
        )
        for i, s in enumerate(["triage", "topology", "deploy_correlation"])
    )
    hyp = make_hypothesis(supporting=evs, next_action="Roll back v412.")
    brief = _mb(hypotheses=(hyp,))
    notifier.post_brief(brief, feedback_channel="C123")
    rendered = repr(fake.posted[0].get("blocks", []))
    assert "finding from triage" in rendered
    assert "finding from topology" in rendered
    assert "finding from deploy_correlation" in rendered


def test_post_brief_truncates_supporting_evidence_above_4(monkeypatch):
    from tests.conftest import make_brief as _mb
    from tests.conftest import make_evidence, make_hypothesis

    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)
    evs = tuple(make_evidence(summary=f"finding #{i}") for i in range(7))
    hyp = make_hypothesis(supporting=evs)
    brief = _mb(hypotheses=(hyp,))
    notifier.post_brief(brief, feedback_channel="C123")
    rendered = repr(fake.posted[0].get("blocks", []))
    assert "3 more findings" in rendered


def test_post_brief_renders_per_hypothesis_next_action_when_different(monkeypatch):
    from tests.conftest import make_brief as _mb
    from tests.conftest import make_hypothesis

    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)
    hyp1 = make_hypothesis(rank=1, next_action="Top recommendation.")
    hyp2 = make_hypothesis(
        rank=2,
        key="cve_exposure",
        title="CVE exposure",
        score=0.55,
        next_action="Different per-hypothesis next step.",
    )
    brief = _mb(hypotheses=(hyp1, hyp2))
    notifier.post_brief(brief, feedback_channel="C123")
    rendered = repr(fake.posted[0].get("blocks", []))
    assert "Different per-hypothesis next step" in rendered


def test_post_brief_footer_includes_live_trace_link_when_base_url_set(monkeypatch):
    monkeypatch.setenv("CAUSAL_ONCALL_BASE_URL", "https://example.run.app")
    notifier = SlackNotifier(_cfg())
    fake = _FakeSlack()
    monkeypatch.setattr(notifier, "_slack", fake, raising=False)
    brief = make_brief(problem_id="P-FOOT")
    notifier.post_brief(brief, feedback_channel="C123")
    rendered = repr(fake.posted[0].get("blocks", []))
    assert "https://example.run.app/trace/P-FOOT" in rendered
    assert "View live trace" in rendered
