"""Recorded Dynatrace MCP cassettes — deterministic replay for the contract suite.

A cassette is a JSON file storing the ordered sequence of MCP calls one
contract test made against the live Dynatrace MCP server, plus the
recorded response (or error) for each. Tests replay cassettes via
``CassetteMCP``; live recording happens via ``scripts/record_cassettes.py``.

This design intentionally avoids the vcrpy library: the MCP server runs
as a stdio subprocess (npx + JSON-RPC framing), not HTTP, so vcrpy's
HTTP-only recorder doesn't apply. A bespoke 50-line replayer is the
straightforward path.
"""
