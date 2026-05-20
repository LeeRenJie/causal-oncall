"""CassetteMCP — replay-only stand-in for the real Dynatrace MCP subprocess.

The DynatraceClient's only seam to MCP is a private ``_mcp`` attribute
satisfying the ``_MCPProcess`` protocol: ``call_tool(name, args) -> dict``
plus ``close()``. CassetteMCP satisfies that protocol by replaying calls
recorded into a JSON file.

Cassette format (a list of records, replayed in order):

    [
        {
            "tool": "execute_dql",
            "args": {"query": "fetch logs | limit 1", "parameters": {}},
            "response": {"records": [{"x": 1}], "executionMs": 12}
        },
        {
            "tool": "get_problem_details",
            "args": {"problem_id": "P-123"},
            "error": {"type": "RateLimited", "message": "429"}
        }
    ]

The replayer is strict: it raises if the call order or args drift from the
recording. This is the intended failure mode — when the MCP server changes
shape, the contract suite must fail loudly so we re-record.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from causal_oncall.domain.exceptions import DynatraceUnavailable, RateLimited

_ERROR_TYPES: dict[str, type[Exception]] = {
    "RateLimited": RateLimited,
    "DynatraceUnavailable": DynatraceUnavailable,
}


class CassetteReplayError(AssertionError):
    """Raised when a test's MCP call sequence diverges from the cassette."""


class CassetteMCP:
    """Replay-only MCP stand-in driven by a cassette JSON file."""

    def __init__(self, cassette_path: Path) -> None:
        self._path = cassette_path
        self._records: list[dict[str, Any]] = json.loads(cassette_path.read_text(encoding="utf-8"))
        self._cursor = 0
        self.calls: list[tuple[str, dict]] = []
        self.closed = 0

    def call_tool(self, name: str, arguments: dict) -> dict:
        if self._cursor >= len(self._records):
            raise CassetteReplayError(
                f"Cassette {self._path.name!r} exhausted at call #{self._cursor + 1}: "
                f"tool={name!r} args={arguments!r}"
            )
        record = self._records[self._cursor]
        self._cursor += 1
        self.calls.append((name, arguments))
        if record["tool"] != name:
            raise CassetteReplayError(
                f"Cassette {self._path.name!r} call #{self._cursor}: expected tool "
                f"{record['tool']!r}, got {name!r}"
            )
        if "error" in record:
            err = record["error"]
            exc_cls = _ERROR_TYPES.get(err["type"], DynatraceUnavailable)
            raise exc_cls(err.get("message", ""))
        return record["response"]

    def close(self) -> None:
        self.closed += 1


def cassette_path(test_name: str) -> Path:
    """Resolve a cassette JSON for a given test name."""
    return Path(__file__).parent / f"{test_name}.json"
