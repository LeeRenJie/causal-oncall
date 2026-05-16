# Causal On-Call

When a production incident fires, the on-call engineer's first fifteen minutes are a manual scavenger hunt through dashboards, logs, recent deploys, and Slack history. Causal On-Call is an Agent Development Kit (ADK) multi-agent system that, the moment a Dynatrace problem opens, runs the exact pre-mortem an experienced SRE would: it converts the problem to a Dynatrace Query Language (DQL) plan, pulls anomaly windows and changepoints via Davis Analyzers, walks the dependency topology to bound blast radius, cross-references the last N deploys, and produces a single Markdown incident brief with a ranked causal hypothesis tree and an explicit next-action recommendation — posted to Slack and as a Dynatrace problem comment within 90 seconds. The agent does not replace the on-call. It gives them the page they would have built by hand at minute fifteen, at minute one.

## Demo

A roughly three-minute walkthrough lives at `docs/demo.gif` (recorded on submission day).

## What it does

- Turns a freshly-opened Dynatrace problem into a ranked causal hypothesis brief in under 90 seconds.
- Visibly replans when the on-call rejects a hypothesis, so the agent's reasoning is auditable, not opaque.
- Short-circuits to a 30-second brief with a "seen this 14 times in 6 months" badge when the incident memory finds a high-confidence prior match.
- Surfaces a rolling self-improvement metric ("top hypothesis correct: 73%, up from 41% in month 1") backed by Arize Phoenix traces and evals.

## Architecture

```
[Webhook: Dynatrace problem.open]
        |
        v
+------------------------+
|  Orchestrator Agent    |   <-- Gemini 3.1 Pro, ADK
|  (plans + delegates)   |
+------------------------+
   |     |        |        |        |
   v     v        v        v        v
[Triage] [Topology] [Deploy-corr] [Anomaly-window] [Vuln/Sec]
   |     |        |        |        |
   +-----+--------+--------+--------+
                  |
                  v
       +-------------------------+
       |  Synthesizer Agent      |   <-- writes Markdown brief, ranks hypotheses
       +-------------------------+
                  |
        +---------+----------+
        v                    v
   Slack post            Dynatrace problem comment
                         (via MCP tool)
```

The orchestrator queries an incident-memory store (MongoDB Atlas Vector Search) before dispatch. On a high-confidence pattern match, it short-circuits the specialist pipeline to a memory-only brief; otherwise it runs all five specialists, aggregates their structured `Evidence`, and hands the bag to the synthesizer. The synthesizer ranks deterministically and uses Gemini only for prose. Every step is traced via the Arize Phoenix OSS SDK; on-call feedback flows back into both the memory store and the Phoenix eval dataset that powers the self-improvement metric.

## Quickstart

```bash
git clone https://github.com/leerenjie/causal-oncall.git
cd causal-oncall
cp .env.example .env  # fill in real values

# Build + run locally
docker build -t causal-oncall .
docker run --rm -p 8080:8080 --env-file .env causal-oncall

# Fire a synthetic problem at the webhook
curl -X POST http://localhost:8080/webhook/dynatrace/problem-open \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/incidents/payment_latency_spike.json
```

The container ships with both Python 3.12 and Node 20, because the Dynatrace MCP server (`@dynatrace-oss/dynatrace-mcp`) is distributed only via `npx`.

## Local development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Run the full test pyramid (unit + integration + e2e; contract skipped
# unless credentials are present)
pytest

# Show line + branch coverage; the suite fails the run if either drops below 100%
pytest --cov-report=term-missing

# Lint + format
ruff check .
black .
```

Pre-commit hooks (`pre-commit install`) run ruff, black, and the unit subset before each `git push`.

## Testing

The suite is a four-layer pyramid:

| Layer       | Lives in                       | Scope                                                                                |
| ----------- | ------------------------------ | ------------------------------------------------------------------------------------ |
| Unit        | `tests/unit/`                  | One module per test file. Boundaries faked. <100ms each.                             |
| Contract    | `tests/contract/`              | The public interface of a module against its real dependency (Dynatrace MCP, Atlas). |
| Integration | `tests/integration/`           | Multiple modules wired together; one real external at a time.                        |
| End-to-end  | `tests/e2e/test_demo_path.py`  | The three-minute demo path replayed against the fixture incident.                    |

The critical-path package is gated at 100% line coverage and 100% branch coverage; the `pytest --cov-fail-under=100` flag enforces it in CI. Boilerplate code is excluded inline with `# pragma: no cover` plus a one-line reason. Mutation testing (`mutmut`) runs weekly and gates at 80% kill rate to catch the "lines executed but nothing meaningfully asserted" failure mode that naive line coverage misses.

## Partner integration

This entry submits in the **Dynatrace** track. The Dynatrace MCP server is the agent's only window into observability data — remove it and the agent has nothing to reason about.

The `DynatraceClient` module wraps `@dynatrace-oss/dynatrace-mcp` and uses these tools:

- `execute_dql` — the dominant call across every specialist for log, event, and metric reads
- `list_problems` and `get_problem_details` — webhook payload validation + context hydration
- `list_analyzers`, `run_changepoint_analyzer`, `run_forecast_analyzer` — Davis Analyzers powering the Anomaly Window specialist
- `get_topology_neighbors` — dependency-graph traversal for the Topology specialist
- `list_vulnerabilities` — newly-active CVE check for the Vuln/Sec specialist
- `post_problem_comment` (write path) — delivers the finalized brief back into the Dynatrace UI

Upstream source: <https://github.com/dynatrace-oss/dynatrace-mcp>.

MongoDB Atlas (incident memory) and Arize Phoenix (tracing + eval) are infrastructure, not partner-bucket integrations. Atlas is reached via the plain `pymongo` driver; Phoenix is the OSS SDK, not its MCP server.

## License

Apache License 2.0 — see [LICENSE](LICENSE) for the full text.
