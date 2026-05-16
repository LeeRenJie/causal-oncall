"""Pure domain types. No I/O, no third-party deps beyond stdlib + pydantic.

Every cross-module signature in causal_oncall speaks one of these types;
that is what lets the I/O modules stay deep without leaking concrete
infrastructure schemas (Mongo docs, MCP JSON, Slack blocks) to callers.
"""
