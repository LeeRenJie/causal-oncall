"""Record Dynatrace MCP cassettes for the contract suite.

Run once in a session with valid Dynatrace OAuth (env vars set, or
browser-OAuth fallback usable). Drives the live MCP server via the
``mcp`` Python SDK, captures the ``(tool, args, response)`` triples
the contract tests assert on, and writes them to
``tests/contract/cassettes/<test_name>.json`` so CI can replay them
without creds.

Usage::

    # one-time install of the SDK in the project venv:
    pip install mcp python-dotenv

    # populate causal-oncall/.env with DT_ENVIRONMENT (+ optional OAUTH_*):
    cp ../spike/.env .env

    # run:
    python scripts/record_cassettes.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

CASSETTE_DIR = Path(__file__).resolve().parent.parent / "tests" / "contract" / "cassettes"


def _record_or_die() -> int:  # pragma: no cover  # human-driven script
    try:
        from dotenv import load_dotenv
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        print(
            "FAIL: missing dependency:",
            exc,
            "\nRun: pip install mcp python-dotenv",
            file=sys.stderr,
        )
        return 1

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    if not os.environ.get("DT_ENVIRONMENT"):
        print("FAIL: DT_ENVIRONMENT not set in .env", file=sys.stderr)
        return 2

    async def _drive() -> int:
        server_params = StdioServerParameters(
            command="npx",
            args=["-y", "@dynatrace-oss/dynatrace-mcp-server@latest"],
            env={**os.environ},
        )

        async with (
            stdio_client(server_params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            # 1. Record a benign DQL — feeds
            #    test_execute_dql_against_real_mcp_returns_a_valid_query_result.
            #
            #    Live Dynatrace MCP v1.8.5 takes ``dqlStatement`` (W2-S0 drift
            #    finding) — NOT the older ``query`` + ``parameters`` shape the
            #    pre-spike code assumed.
            dql_args = {"dqlStatement": "fetch logs | limit 1"}
            dql_call = await session.call_tool("execute_dql", arguments=dql_args)
            dql_response = _extract_payload(dql_call)
            _write_cassette(
                "test_execute_dql_against_real_mcp_returns_a_valid_query_result",
                [{"tool": "execute_dql", "args": dql_args, "response": dql_response}],
            )

            # 2. Record a list_problems + two hydration DQLs — feeds
            #    test_get_problem_context_handles_known_test_problem_id.
            #
            #    Drift: MCP v1.8.5 does NOT expose ``get_problem_details``.
            #    The closest read is ``list_problems`` (returns prose summary
            #    of recent problems). When the tenant has no open problems
            #    the live MCP returns the literal string ``"No problems found"``
            #    — captured into the cassette so replay tests degrade
            #    deterministically (same path empty trial tenants will hit).
            problems = await session.call_tool("list_problems", arguments={})
            problems_payload = _extract_payload(problems)
            problem_id = _pick_problem_id(problems_payload)
            if problem_id is None:
                print(
                    "WARN: tenant has zero open problems; using fallback "
                    "synthetic problem id. Re-run when a problem is open.",
                )
                problem_id = "PROBLEM-CASSETTE-001"

            ctx_calls = []
            for tool, args in [
                ("list_problems", {}),
                (
                    "execute_dql",
                    {
                        "dqlStatement": (
                            f'fetch dt.davis.problems | filter problem.id == "{problem_id}"'
                        )
                    },
                ),
                (
                    "execute_dql",
                    {"dqlStatement": (f'fetch events | filter problem.id == "{problem_id}"')},
                ),
            ]:
                call = await session.call_tool(tool, arguments=args)
                ctx_calls.append({"tool": tool, "args": args, "response": _extract_payload(call)})
            # Drift-isolated filename: the live cassette is parked under
            # ``_live_get_problem_context.json`` so the active contract
            # test (which still drives the old ``get_problem_details``
            # tool name) continues to replay against the synthetic
            # cassette committed under
            # ``test_get_problem_context_handles_known_test_problem_id.json``.
            # When the DynatraceClient gets rewired to use ``list_problems``
            # in a follow-up slice, swap the filename here back to the
            # canonical test name.
            _write_cassette("_live_get_problem_context", ctx_calls)

            print(
                f"\nRecorded cassettes to {CASSETTE_DIR}. "
                "Commit them to lock the contract suite to the current MCP shape."
            )
            return 0

    return asyncio.run(_drive())


def _extract_payload(call_result) -> dict:  # pragma: no cover
    if call_result.isError:
        raise RuntimeError(f"MCP call failed: {[c for c in call_result.content]}")
    text = "\n".join(
        getattr(c, "text", "") for c in call_result.content if getattr(c, "text", None)
    )
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


def _pick_problem_id(payload) -> str | None:  # pragma: no cover
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return first.get("problemId") or first.get("id")
    if isinstance(payload, dict):
        items = payload.get("problems") or payload.get("items") or []
        if items:
            return items[0].get("problemId") or items[0].get("id")
    return None


def _write_cassette(test_name: str, records: list[dict]) -> None:  # pragma: no cover
    CASSETTE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CASSETTE_DIR / f"{test_name}.json"
    out_path.write_text(json.dumps(records, indent=4), encoding="utf-8")
    print(f"  wrote {out_path}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_record_or_die())
