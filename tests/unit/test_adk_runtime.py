"""TDD spec for the ADK runtime seam.

This module is the compliance heart of the project: the orchestrator and
specialists must run *through* the ADK runtime (LlmAgent + FunctionTool +
McpToolset + Runner + SessionService), not a hand-rolled loop, and Gemini
prose must be produced by the ADK runtime rather than a direct
``google.genai.generate_content`` call.

The tests stay hermetic by injecting a fake ``BaseLlm`` (ADK's documented
model seam) so no network / Vertex call ever fires. The orchestration
*logic* (ranking, memory routing, replan) is tested elsewhere and is
unaffected; here we test the ADK glue itself: that the specialists are
exposed as real ADK FunctionTools, that the synthesizer prose runs the
ADK Runner, and that the orchestrator agent is a real LlmAgent carrying
the specialist tools.
"""

from __future__ import annotations

import json

from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.tools.function_tool import FunctionTool
from google.genai import types as genai_types

from causal_oncall.adk_runtime import (
    AdkLlmSynthesisCall,
    AdkPatternSynthesisClient,
    build_orchestrator_agent,
    build_specialist_tools,
    run_text_agent,
)
from causal_oncall.domain.evidence import Evidence
from causal_oncall.specialists.base import Specialist
from tests.conftest import FakeDynatraceClient, make_evidence, make_signature


class _FixedTextLlm(BaseLlm):
    """A fake ADK model that always yields one canned text response.

    This is the ADK-native test double the plan calls for: substituting a
    ``BaseLlm`` keeps the Runner code path real while the model round-trip
    stays in-process.
    """

    reply: str = "{}"

    async def generate_content_async(self, llm_request, stream=False):
        yield LlmResponse(
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part(text=self.reply)],
            )
        )


class _StubSpecialist(Specialist):
    name = "stub"

    def __init__(self, name: str, evidence: Evidence) -> None:
        super().__init__(dynatrace=FakeDynatraceClient())  # type: ignore[arg-type]
        self.name = name
        self._evidence = evidence
        self.calls = 0

    def investigate(self, signature, *, prior_hypothesis=None):
        self.calls += 1
        return self._evidence


def test_specialists_are_exposed_as_real_adk_function_tools():
    """Each specialist becomes an ADK FunctionTool named for the specialist."""
    triage = _StubSpecialist("triage", make_evidence(specialist="triage"))
    topology = _StubSpecialist("topology", make_evidence(specialist="topology"))
    tools = build_specialist_tools([triage, topology])

    assert all(isinstance(t, FunctionTool) for t in tools)
    assert [t.name for t in tools] == ["investigate_triage", "investigate_topology"]
    # The wrapped tool carries a non-empty description so the LLM planner
    # can choose it (ADK reads the docstring as the function declaration).
    assert all(t.description for t in tools)


def test_function_tool_invocation_runs_the_underlying_specialist():
    """Calling the wrapped tool dispatches the real specialist and returns its evidence."""
    triage = _StubSpecialist(
        "triage", make_evidence(specialist="triage", hypothesis_key="db_pool_exhaustion")
    )
    [tool] = build_specialist_tools([triage])
    sig = make_signature()
    result = tool.func(problem_id=sig.problem_id, _signature=sig)
    assert triage.calls == 1
    assert result["specialist"] == "triage"
    assert result["hypothesis_key"] == "db_pool_exhaustion"


def test_orchestrator_agent_is_an_llm_agent_carrying_the_specialist_tools():
    """The production orchestrator is a real ADK LlmAgent with the specialist tools attached."""
    triage = _StubSpecialist("triage", make_evidence(specialist="triage"))
    topology = _StubSpecialist("topology", make_evidence(specialist="topology"))
    agent = build_orchestrator_agent(
        model="gemini-2.5-pro",
        specialist_tools=build_specialist_tools([triage, topology]),
    )
    assert isinstance(agent, LlmAgent)
    assert agent.model == "gemini-2.5-pro"
    tool_names = {t.name for t in agent.tools}
    assert {"investigate_triage", "investigate_topology"} <= tool_names


def test_orchestrator_agent_can_carry_extra_mcp_toolset():
    """An extra toolset (the Dynatrace McpToolset in production) is registered alongside.

    The sentinel is a real ADK ``BaseToolset`` subclass — the same base
    class ``McpToolset`` extends — so it passes the LlmAgent tool
    validation the production Dynatrace toolset would.
    """
    from google.adk.tools.base_toolset import BaseToolset

    class _SentinelToolset(BaseToolset):
        async def get_tools(self, readonly_context=None):
            return []

        async def close(self):
            return None

    sentinel = _SentinelToolset()
    triage = _StubSpecialist("triage", make_evidence(specialist="triage"))
    agent = build_orchestrator_agent(
        model="gemini-2.5-pro",
        specialist_tools=build_specialist_tools([triage]),
        extra_toolsets=[sentinel],
    )
    assert sentinel in agent.tools


def test_run_text_agent_drives_the_adk_runner_and_returns_text():
    """``run_text_agent`` executes a real Runner + SessionService round-trip."""
    agent = LlmAgent(
        model=_FixedTextLlm(model="fake", reply="hello from adk runner"),
        name="prose_agent",
        instruction="echo",
    )
    out = run_text_agent(agent, "produce prose")
    assert out == "hello from adk runner"


def test_run_text_agent_concatenates_multi_part_final_response():
    """Multi-part final responses are concatenated into one string."""

    class _MultiPartLlm(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(
                content=genai_types.Content(
                    role="model",
                    parts=[
                        genai_types.Part(text="part-a "),
                        genai_types.Part(text="part-b"),
                    ],
                )
            )

    agent = LlmAgent(model=_MultiPartLlm(model="fake"), name="p", instruction="x")
    assert run_text_agent(agent, "go") == "part-a part-b"


def test_run_text_agent_returns_empty_string_when_model_yields_no_text():
    """A response whose final parts carry no text collapses to an empty string."""

    class _EmptyLlm(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            yield LlmResponse(content=genai_types.Content(role="model", parts=[]))

    agent = LlmAgent(model=_EmptyLlm(model="fake"), name="p", instruction="x")
    assert run_text_agent(agent, "go") == ""


def test_adk_synthesis_call_produces_json_via_the_adk_runtime():
    """The synthesizer's LLM seam parses JSON produced by an ADK LlmAgent run.

    This is the proof that Gemini prose is generated *through the ADK
    runtime* — no ``google.genai.generate_content`` call. The call object
    is what the production synthesizer assigns to ``_llm_call``.
    """
    payload = {
        "hypotheses": {"db_pool_exhaustion": {"title": "DB pool", "next_action": "Roll back"}}
    }
    call = AdkLlmSynthesisCall(
        model=_FixedTextLlm(model="fake", reply=json.dumps(payload)),
        agent_name="synthesizer",
        instruction="Return JSON.",
    )
    result = call("any prompt")
    assert result == payload


def test_adk_synthesis_call_strips_markdown_fences_before_parsing():
    """Gemini often wraps JSON in ```json fences; the call must tolerate that."""
    fenced = "```json\n" + json.dumps({"hypotheses": {}}) + "\n```"
    call = AdkLlmSynthesisCall(
        model=_FixedTextLlm(model="fake", reply=fenced),
        agent_name="synthesizer",
        instruction="Return JSON.",
    )
    assert call("prompt") == {"hypotheses": {}}


def test_pattern_synthesis_client_runs_curator_synthesis_through_adk():
    """The Curator's Gemini seam (synthesize_pattern/token_counts) runs via ADK."""
    payload = {"name": "DB pool exhaustion pattern", "signals": ["max_connections at ceiling"]}
    client = AdkPatternSynthesisClient(
        model=_FixedTextLlm(model="fake", reply=json.dumps(payload)),
    )
    assert client.synthesize_pattern("cluster prompt") == payload
    # token_counts conforms to the protocol shape the Curator reads.
    assert client.token_counts() == (0, 0)


def test_strip_json_fences_handles_a_lone_fence_marker():
    """A single ``` line (no body, no closing fence) collapses to empty text.

    Exercises the fence-guard branches where, after dropping the opening
    fence line, ``lines`` is empty — so the closing-fence guard's first
    operand short-circuits. Keeps the JSON-fence handling fully covered.
    """
    from causal_oncall.adk_runtime import _strip_json_fences

    assert _strip_json_fences("```") == ""


def test_strip_json_fences_passes_through_unfenced_text():
    """Plain JSON without a fence is returned untouched."""
    from causal_oncall.adk_runtime import _strip_json_fences

    assert _strip_json_fences('{"hypotheses": {}}') == '{"hypotheses": {}}'


def test_strip_json_fences_handles_open_fence_without_a_closing_fence():
    """An opening ```json fence with a body but no closing fence still parses.

    After dropping the opening fence line, ``lines`` is non-empty but its
    last line is NOT a closing ``` — exercises the closing-fence guard's
    false branch so both directions of that guard are covered.
    """
    from causal_oncall.adk_runtime import _strip_json_fences

    assert _strip_json_fences('```json\n{"hypotheses": {}}') == '{"hypotheses": {}}'
