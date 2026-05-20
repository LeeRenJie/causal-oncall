"""TDD spec for Brief + Hypothesis.

The brief is the single artifact judges see — its Markdown output must
be deterministic, must surface the top recommendation prominently, and
must list hypotheses in rank order.
"""

from __future__ import annotations

from tests.conftest import make_brief, make_evidence, make_hypothesis


def test_to_markdown_includes_problem_id_in_heading():
    brief = make_brief(problem_id="P-007")
    md = brief.to_markdown()
    assert "P-007" in md


def test_to_markdown_lists_hypotheses_in_rank_order():
    h1 = make_hypothesis(key="a", title="A wins", rank=1, score=0.9)
    h2 = make_hypothesis(key="b", title="B is runner-up", rank=2, score=0.5)
    brief = make_brief(hypotheses=(h1, h2))
    md = brief.to_markdown()
    assert md.index("A wins") < md.index("B is runner-up")


def test_to_markdown_surfaces_top_recommendation():
    brief = make_brief(
        hypotheses=(make_hypothesis(next_action="Roll back deploy v412 on payment-service."),)
    )
    md = brief.to_markdown()
    assert "Roll back deploy v412 on payment-service." in md


def test_to_markdown_marks_memory_short_circuit_runs():
    brief = make_brief(memory_short_circuit=True)
    md = brief.to_markdown()
    # The badge must be visible enough that a judge scanning the brief
    # sees the "we've seen this before" wow moment.
    assert "seen" in md.lower() or "memory" in md.lower()


def test_to_markdown_renders_supporting_evidence_under_its_hypothesis():
    ev = make_evidence(summary="5xx burst on /charge starting 09:28:00")
    h = make_hypothesis(supporting=(ev,))
    brief = make_brief(hypotheses=(h,))
    md = brief.to_markdown()
    assert "5xx burst on /charge starting 09:28:00" in md


def test_to_markdown_renders_refuting_evidence_when_present():
    ref = make_evidence(
        summary="No deploys in window — refutes deploy-correlation",
        stance="refutes",
        confidence=0.4,
    )
    h = make_hypothesis(refuting=(ref,))
    brief = make_brief(hypotheses=(h,))
    md = brief.to_markdown()
    assert "Refuting evidence" in md
    assert "No deploys in window" in md


def test_to_markdown_renders_supporting_evidence_with_dynatrace_links():
    ev = make_evidence(
        summary="5xx burst",
        links=("https://abc.live.dynatrace.com/ui/problems/P-007",),
    )
    h = make_hypothesis(supporting=(ev,))
    brief = make_brief(hypotheses=(h,))
    md = brief.to_markdown()
    assert "[Open in Dynatrace]" in md
    assert "abc.live.dynatrace.com" in md


def test_to_markdown_lists_unresolved_questions_when_present():
    brief = make_brief()
    from causal_oncall.domain.brief import Brief

    enriched = Brief(
        problem_id=brief.problem_id,
        generated_at=brief.generated_at,
        ranked_hypotheses=brief.ranked_hypotheses,
        top_recommendation=brief.top_recommendation,
        memory_short_circuit=brief.memory_short_circuit,
        unresolved_questions=("Was the deploy rolled back?",),
    )
    md = enriched.to_markdown()
    assert "Open questions for the on-call" in md
    assert "Was the deploy rolled back?" in md
