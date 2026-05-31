# Causal On-Call

> Turn a Dynatrace problem into a ranked SRE incident brief in 90 seconds. A multi-agent system built on Google ADK, Dynatrace MCP as its central nervous system, and a memory that compounds.

**Live demo:** <https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true>
**Track:** Dynatrace (Google Cloud Rapid Agent Hackathon, 2026)
**License:** Apache-2.0
**Status:** 318 tests passing, 100% line + 100% branch coverage on the critical-path package.

---

## The problem

Every on-call engineer's first fifteen minutes look the same. A P1 problem fires. You jump between dashboards, logs, deploy history, and Slack threads. Fifteen minutes later you have a working theory the data already supported. The information was always in Dynatrace; it just took a senior SRE's pattern matching to pull the right threads in the right order.

Causal On-Call gives that pattern matching to every on-call engineer, so the first page they see is the page they used to build by hand at minute fifteen.

## What it does

When a Dynatrace `problem.open` webhook fires:

1. The orchestrator (a Google ADK `LlmAgent` running on the ADK `Runner`, model `gemini-2.5-pro`) normalizes the problem into a stable signature and queries an incident memory store (MongoDB Atlas Vector Search) for a high-confidence prior match.
2. On a hit, it short-circuits the investigation and returns a ~30-second brief with a "seen this N times in M months" badge and the proven fix prefilled.
3. Otherwise it dispatches five specialists (Triage, Topology, Deploy Correlation, Anomaly Window, Vuln/Sec), each exposed to the orchestrator agent as an ADK `FunctionTool`, sequenced to respect the 50-req/min rate limit. The Dynatrace MCP server is wired into the same agent as an ADK `McpToolset`, so the specialists' window into observability data is a genuine ADK tool surface.
4. The synthesizer aggregates the structured `Evidence` from each specialist, ranks hypotheses deterministically using `0.4·supporting_count + 0.4·mean_confidence + 0.2·specialist_trust`, then drafts prose by routing through the ADK runtime (`AdkLlmSynthesisCall` drives an ADK `LlmAgent` + `Runner`, not a direct `google.genai` call), and emits a single Markdown brief with clickable Dynatrace links on every piece of evidence.
5. The brief is delivered to Slack and written back into Dynatrace via the MCP. One-click feedback from the on-call flows back into both the memory store and the Phoenix eval dataset that powers the rolling self-improvement metric.

## Four wow moments in the 3-minute demo

| # | Beat | What the judge sees |
|---|---|---|
| 1 | Cold incident to 90-second ranked brief | A fresh webhook produces a Markdown brief with ranked hypotheses + supporting Dynatrace links inside the 90-second target ([wow1 backup](demo/wow_backups/wow1_cold_incident_brief.png)) |
| 2 | Live trace UI shows the agent thinking | Server-Sent Events stream every specialist's dispatch + completion in real time, so the agent's plan is auditable mid-flight ([wow2 backup](demo/wow_backups/wow2_hypothesis_rejection.png)) |
| 3 | Pre-flight memory hit short-circuits | "Seen this 14 times in 6 months" badge + proven fix; institutional tribal knowledge becomes structured and survives turnover ([wow3 backup](demo/wow_backups/wow3_memory_match_short_circuit.png)) |
| 4 | Self-improvement dashboard | Rolling top-hypothesis accuracy curve climbing 41% to 73% over the simulated 6-month history, backed by Arize Phoenix traces ([wow4 backup](demo/wow_backups/wow4_dashboard_curve.png)) |

Full narration in [`demo/SCRIPT.md`](demo/SCRIPT.md). Dry-run protocol in [`demo/dry-run-checklist.md`](demo/dry-run-checklist.md).

**About the live URL (read this if you also read the code):** the production wiring is genuinely ADK. The orchestrator is a real ADK `LlmAgent` on the `Runner`, the specialists are real `FunctionTool`s, Dynatrace MCP is a real `McpToolset`, and Gemini synthesis routes through the ADK runtime (all in `src/causal_oncall/adk_runtime.py`, selected by `_build_production_wiring()` in `app.py`). The public demo URL, however, runs with `DEMO_MODE` set: a deterministic faked replay of that same architecture, chosen for speed, cost, and reliability during judging. The one exception is Slack, which posts for real. So if you read the code and see `DEMO_MODE` gating in `app.py`, that is expected, and this paragraph is the disclosure. To exercise the real ADK + Gemini + MCP path, run the production wiring locally with credentials per the Quickstart below.

## Architecture

```
[Webhook: Dynatrace problem.open]
        |
        v
+--------------------------------+
|  Orchestrator (ADK LlmAgent)   |   gemini-2.5-pro, run on ADK Runner
|  (plans + delegates via tools) |
+--------------------------------+
   |  pre-flight memory match (Mongo Atlas Vector Search)
   |
   |  ADK FunctionTools (one per specialist)        ADK McpToolset
   |     |        |        |        |               (Dynatrace MCP,
   v     v        v        v        v                read-only filter)
[Triage] [Topology] [Deploy-corr] [Anomaly-window] [Vuln/Sec]
   |     |        |        |        |     <- observability data via the
   +-----+--------+--------+--------+         Dynatrace McpToolset
                  |                            (sequenced for 50/min rate limit)
                  v
       +----------------------------------+
       |  Synthesizer                     |   deterministic ranking, then
       |  (deterministic rank + ADK prose)|   prose via ADK LlmAgent + Runner
       +----------------------------------+   (gemini-2.5-pro)
                  |
        +---------+----------+----------------+
        v                    v                v
   Slack post     Dynatrace write-back   Incident memory
                  (via MCP)              (Mongo Atlas)
                                              |
                                              v
                                       Arize Phoenix
                                       (traces + evals)
                                              |
                                              v
                                       /dashboard
                                       (rolling accuracy)
```

The orchestration logic (3-tier memory routing, deterministic hypothesis ranking, hypothesis-rejection replan, Dynatrace write-back) lives in a deterministic `Orchestrator` class. That is what makes the system testable at 100% branch coverage and replayable for the demo. The ADK runtime is the agent face of the same six-agent investigation: the orchestrator is a real `google.adk.agents.LlmAgent` on the ADK `Runner`, the five specialists are real `google.adk.tools.function_tool.FunctionTool`s attached to it, the Dynatrace MCP server is a real `google.adk.tools.mcp_tool.McpToolset`, and the synthesizer prose step routes through the ADK runtime (`AdkLlmSynthesisCall`) rather than a direct `google.genai.generate_content` call. See `src/causal_oncall/adk_runtime.py` for the wiring. Model routing: `gemini-2.5-pro` for the orchestrator and synthesizer, `gemini-3.5-flash` for the specialists.

**Partner bucket claim:** Dynatrace MCP is the load-bearing partner integration, and it is wired as an ADK `McpToolset` (the `StdioConnectionParams` + npx `@dynatrace-oss/dynatrace-mcp-server` pattern, `timeout=90`, restricted to a read-only tool filter). It is the orchestrator agent's only window into observability data. Remove it and the agent has nothing to reason about. MongoDB Atlas (memory) is reached via the plain `pymongo` driver, infrastructure rather than a competing bucket claim. Arize Phoenix is the OSS SDK, also infrastructure.

### Tech stack

- **Google ADK (Agent Development Kit)** as the orchestration framework: `LlmAgent` orchestrator on the ADK `Runner`, specialists as `FunctionTool`s, Dynatrace MCP as an `McpToolset`, Gemini synthesis routed through the ADK runtime.
- **Gemini 2.5 Pro** (orchestrator + synthesizer) and **Gemini 3.5 Flash** (specialists).
- **Dynatrace MCP** via an ADK `McpToolset` (the partner-bucket integration).
- **MongoDB Atlas** for vector memory (768-dim `text-embedding-005` embeddings via `vertexai.language_models.TextEmbeddingModel`).
- **Arize Phoenix** for traces and the rolling self-improvement eval.
- **Cloud Run** for hosting and **Secret Manager** for the Mongo URI and Dynatrace OAuth credentials.

### Dynatrace MCP tools used

- `list_problems`: webhook payload context hydration.
- `execute_dql`: the dominant call across every specialist for log, event, and metric reads (Davis CoPilot composes DQL from the specialist's intent; hand-written fallbacks in `src/causal_oncall/specialists/_dql_fallbacks.py` cover the misfires). Topology neighbours and problem context are also fetched via Grail DQL.
- `list_davis_analyzers`, `execute_davis_analyzer`: Davis Analyzers powering the Anomaly Window specialist.
- `list_vulnerabilities`: newly-active CVE check for the Vuln/Sec specialist.
- `send_event` (write path): investigation lifecycle and brief write-back posted as Dynatrace custom events so the timeline shows the agent's audit trail.

These are exposed to the orchestrator agent through a single ADK `McpToolset` with a read-only `tool_filter`. Upstream MCP source: <https://github.com/dynatrace-oss/dynatrace-mcp>.

## Quickstart

```bash
git clone https://github.com/LeeRenJie/causal-oncall.git
cd causal-oncall
cp .env.example .env  # fill in real values for production wiring

# Option A: Docker (recommended; includes the Node 20 runtime for the MCP server)
docker build -t causal-oncall .
docker run --rm -p 8080:8080 --env-file .env causal-oncall

# Option B: local Python dev (also requires Node 20 for the MCP server)
python -m venv .venv && source .venv/bin/activate  # or .venv/Scripts/activate on Windows
pip install -e ".[dev]"
uvicorn causal_oncall.app:app --port 8080

# Fire a synthetic problem at the webhook
curl -X POST http://localhost:8080/webhook/dynatrace-problem \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/incidents/payment_latency_spike.json
```

### Required environment

| Variable | Required for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | LLM calls (Synthesizer, Orchestrator, Specialists) | Or set `GOOGLE_GENAI_USE_VERTEXAI=TRUE` + ADC for Vertex AI |
| `DT_ENVIRONMENT` | Dynatrace MCP | Tenant URL, e.g. `https://abc12345.live.dynatrace.com` |
| `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` | Dynatrace MCP from a non-interactive runtime | Create in Account Management, OAuth clients |
| `MONGODB_URI` | Memory store | Atlas SRV string |
| `SLACK_BOT_TOKEN` | Slack notifier | Optional; brief still posts to Dynatrace + disk without it |
| `CAUSAL_ONCALL_DEMO_MODE=true` | Demo/judges path | Bypasses all external dependencies with in-process fakes |

Cloud Run wires `MONGODB_URI` and the OAuth pair via Google Secret Manager. See `BUILD-LOG.md` W4-S1 for the exact `gcloud run deploy` invocation.

## Testing

```bash
# Full pyramid: unit + integration + contract (live-only contracts skipped without credentials)
pytest -q

# 100% line + branch coverage gate
pytest --cov-branch --cov-fail-under=100

# Mutation testing (weekly cadence; 80% kill rate gate)
mutmut run --paths-to-mutate src/causal_oncall/

# Lint + format
ruff check src tests scripts
black --check src tests scripts
```

| Layer | Lives in | Scope |
|---|---|---|
| Unit | `tests/unit/` | One module per file, all boundaries faked, <100ms each |
| Contract | `tests/contract/` | Public interface vs real dependency (Dynatrace MCP cassettes, Atlas, Slack) |
| Integration | `tests/integration/` | Multiple modules wired together; one real external at a time |
| E2E | `tests/e2e/test_demo_path.py` | The 3-minute demo path replayed against the canonical fixture |

The critical-path package is gated at 100% line + 100% branch coverage; the gate fails CI if either drops. `# pragma: no cover` is allowed only with a one-line inline justification. Mutation testing runs weekly to catch the "lines executed but nothing meaningfully asserted" failure mode that line coverage alone misses.

## Repository layout

```
causal-oncall/
├── src/causal_oncall/
│   ├── app.py                 # FastAPI + wiring (production ADK + demo)
│   ├── adk_runtime.py         # ADK seam: LlmAgent + FunctionTool + McpToolset + Runner
│   ├── orchestrator.py        # Pre-flight memory match -> dispatch -> synthesize
│   ├── synthesizer.py         # Deterministic ranking + ADK-routed Gemini prose
│   ├── specialists/           # 5 specialists, one Specialist.investigate() contract
│   ├── dynatrace_client.py    # Sole wrapper over the Dynatrace MCP server
│   ├── memory_store.py        # Mongo Atlas + vector search
│   ├── phoenix_tracer.py      # OTLP spans + outcome store
│   ├── slack_notifier.py      # Block Kit + feedback button
│   ├── dashboard.py           # Rolling self-improvement metric
│   ├── trace_routes.py        # SSE for the live trace UI
│   └── _demo_wiring.py        # In-process fakes for the judges' demo path
├── tests/                     # 318 tests, 100% line + branch coverage gate
├── demo/                      # Demo script + dry-run checklist + wow_backups
├── scripts/                   # seed_memory.py + ops scripts
├── DEVPOST.md                 # Submission body
├── ROADMAP.md                 # Out-of-scope items that survived the cut
├── BUILD-LOG.md               # Slice-by-slice audit trail
├── Dockerfile                 # Python 3.12 + Node 20 multi-stage
└── pyproject.toml
```

## Engineering principles

Two non-negotiable rules drove every commit:

1. **Test-driven development.** Critical-path code lands red-then-green. Naming the test for the requirement it encodes is enforced (see `ENGINEERING-PRINCIPLES.md`).
2. **Deep modules** (Ousterhout). `Specialist.investigate(signature)` returning `Evidence` is the only public method each specialist exposes; DQL composition, retries, and ranking are private. Same for `DynatraceClient`, `MemoryStore`, `Synthesizer`, and `adk_runtime` (four public functions plus the `AdkLlmSynthesisCall` adapter).

Slice-by-slice audit trail in `BUILD-LOG.md`. Every entry records what shipped vs what was planned, what got cut, and which decisions were deliberate (and traceable) vs accidental drift.

## License

Apache License 2.0. See [LICENSE](LICENSE) for the full text.
