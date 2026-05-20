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


# ---------- W3-S2: from_memory + pattern_match_score fields ---------- #


def test_brief_defaults_from_memory_to_false_and_pattern_match_score_to_none():
    """Cold-start briefs carry the W3-S2 fields at their conservative defaults."""
    brief = make_brief()
    assert brief.from_memory is False
    assert brief.pattern_match_score is None


def test_brief_from_memory_with_pattern_match_score_constructs_cleanly():
    """A memory-hit brief sets both fields together."""
    from causal_oncall.domain.brief import Brief

    brief = Brief(
        problem_id="P-001",
        generated_at=make_brief().generated_at,
        ranked_hypotheses=(),
        top_recommendation="Apply known fix.",
        memory_short_circuit=True,
        from_memory=True,
        pattern_match_score=0.92,
    )
    assert brief.from_memory is True
    assert brief.pattern_match_score == 0.92


def test_brief_rejects_from_memory_without_pattern_match_score():
    """Invariant: from_memory implies the score that triggered it is recorded."""
    import pytest

    from causal_oncall.domain.brief import Brief

    with pytest.raises(ValueError, match="pattern_match_score"):
        Brief(
            problem_id="P-001",
            generated_at=make_brief().generated_at,
            ranked_hypotheses=(),
            top_recommendation="x",
            from_memory=True,
        )


def test_brief_rejects_pattern_match_score_without_from_memory():
    """Inverse invariant: score is only meaningful when from_memory=True."""
    import pytest

    from causal_oncall.domain.brief import Brief

    with pytest.raises(ValueError, match="from_memory"):
        Brief(
            problem_id="P-001",
            generated_at=make_brief().generated_at,
            ranked_hypotheses=(),
            top_recommendation="x",
            pattern_match_score=0.92,
        )


def test_brief_rejects_pattern_match_score_outside_unit_interval():
    """Score is a cosine similarity — must live in [0.0, 1.0]."""
    import pytest

    from causal_oncall.domain.brief import Brief

    with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
        Brief(
            problem_id="P-001",
            generated_at=make_brief().generated_at,
            ranked_hypotheses=(),
            top_recommendation="x",
            from_memory=True,
            pattern_match_score=1.42,
        )


def test_brief_schema_version_is_at_least_two():
    """W3-S2 bumped schema_version; downstream readers key off this constant."""
    from causal_oncall.domain.brief import Brief

    assert Brief.SCHEMA_VERSION >= 2


def test_brief_markdown_footer_advertises_schema_version():
    """The rendered brief carries the schema version in its footer for traceability."""
    brief = make_brief()
    md = brief.to_markdown()
    assert f"schema v{brief.SCHEMA_VERSION}" in md
