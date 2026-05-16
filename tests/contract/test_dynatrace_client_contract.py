"""Contract tests for DynatraceClient against the real MCP server.

Runs only when ``DYNATRACE_OAUTH_CLIENT_ID`` is present in the env;
otherwise auto-skipped. CI does not set these — they are gated to
the builder's machine + the Day-0 spike account.

The point of these tests is to catch the failure mode unit fakes
cannot: the MCP server changes shape between releases. When the
shape drifts, this suite is the first to fail.
"""

from __future__ import annotations

import os

import pytest

pytestmark = [
    pytest.mark.requires_creds,
    pytest.mark.skipif(
        not os.environ.get("DYNATRACE_OAUTH_CLIENT_ID"),
        reason="Dynatrace credentials not set; contract suite gated on real env",
    ),
]


def test_execute_dql_against_real_mcp_returns_a_valid_query_result():
    """Run a no-op DQL through the live MCP server and validate the response shape."""
    raise NotImplementedError(
        "Spin up DynatraceClient with env-driven config, call execute_dql with "
        "a benign query (e.g. `fetch logs | limit 1`), and assert the QueryResult "
        "satisfies the typed contract."
    )


def test_get_problem_context_handles_known_test_problem_id():
    """Seed a known synthetic problem in the spike tenant, fetch its context."""
    raise NotImplementedError(
        "Fetch the spike tenant's seeded synthetic problem and validate that "
        "ProblemContext hydrates without raising."
    )
