# Causal On-Call — Devpost submission

**Track:** Dynatrace
**Live URL:** <https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true>
**Repo:** <https://github.com/LeeRenJie/causal-oncall>

## Inspiration

Every on-call engineer's first fifteen minutes look the same. A P1 problem fires. You jump between dashboards, logs, deploy history, and Slack threads. Fifteen minutes later you have a working theory the data already supported. The pattern matching is a senior SRE skill — it just needs to be made available to the rest of the team without putting the senior on the page. So we asked: what if the first message in the incident channel was already the page the senior would have built by hand at minute fifteen?

## What it does

When a Dynatrace `problem.open` webhook fires, Causal On-Call:

1. **Normalizes** the problem into a stable signature (problem ID, severity, affected entities, opened-at, deterministic fingerprint).
2. **Queries an incident memory store** (MongoDB Atlas Vector Search) for a high-confidence match against past resolved incidents. On a hit, it short-circuits the full investigation and returns a roughly 30-second brief with a "seen this 14 times in 6 months" badge and the proven fix prefilled.
3. **Otherwise dispatches five specialists** (Triage, Topology, Deploy Correlation, Anomaly Window, Vuln/Sec), each exposed to the orchestrator ADK agent as a `FunctionTool` and sequenced to respect the 50-request-per-minute rate limit. The Dynatrace MCP server is wired into the same agent as an ADK `McpToolset`.
4. **Aggregates structured evidence** and hands it to a synthesizer that ranks hypotheses deterministically (`0.4·supporting_count + 0.4·mean_confidence + 0.2·specialist_trust`) and uses Gemini only for prose, routed through the ADK runtime. Every supporting fact carries a clickable link back to the Dynatrace UI.
5. **Delivers the brief** to Slack and writes it back into Dynatrace via the MCP. One-click feedback from the on-call closes the loop back into the memory store and the Phoenix eval dataset that powers the rolling self-improvement metric.

The agent does not replace the on-call. It gives them, at minute one, the page they used to build by hand at minute fifteen.

## How we built it

- **Orchestration framework:** Google ADK (Agent Development Kit). The orchestrator is a real `google.adk.agents.LlmAgent` driven by an ADK `Runner` (with a `SessionService`), not a hand-rolled loop. The five specialists (triage, topology, deploy_correlation, anomaly_window, vuln_sec) are real `google.adk.tools.function_tool.FunctionTool`s attached to that agent. The Dynatrace MCP server is wired in as a real `google.adk.tools.mcp_tool.McpToolset`. Every Gemini round-trip goes through the ADK `Runner` rather than a direct `google.genai.generate_content` call. The wiring lives in `src/causal_oncall/adk_runtime.py`; its public surface is four functions (`build_specialist_tools`, `build_orchestrator_agent`, `build_dynatrace_toolset`, `run_text_agent`) plus the `AdkLlmSynthesisCall` adapter the synthesizer plugs into its LLM seam.
- **Reasoning brain:** Gemini 2.5 Pro for the orchestrator and synthesizer; Gemini 3.5 Flash for the specialists, to keep per-incident LLM cost low.
- **Deterministic core under the ADK runtime:** the orchestration logic (3-tier memory routing, deterministic hypothesis ranking, hypothesis-rejection replan, Dynatrace write-back) lives in a pure `Orchestrator` class. The ADK runtime is the agent face of that same six-agent investigation. Keeping the logic deterministic is what lets the system stay 100% branch-covered and demo-replayable while still expressing every step through genuine ADK primitives.
- **Partner integration (load-bearing):** the Dynatrace MCP server `@dynatrace-oss/dynatrace-mcp-server`, wired as an ADK `McpToolset` over `StdioConnectionParams` (npx launch, 90s handshake timeout, restricted to a read-only tool filter). The orchestrator agent's only window into Dynatrace is that toolset, plus the `DynatraceClient` module that the deterministic dispatch path uses (it paces requests for the 50/min sliding window and maps transport errors to domain exceptions). The tools used cover `execute_dql`, `list_problems`, `list_davis_analyzers`, `execute_davis_analyzer`, `list_vulnerabilities`, and the Grail write-back path.
- **Memory:** MongoDB Atlas. Vector search (768-dim `text-embedding-005` embeddings via `vertexai.language_models.TextEmbeddingModel`) for the pre-flight pattern match, regular operational reads/writes for the incident record store.
- **Observability + evals:** Arize Phoenix OSS SDK. Every agent run produces a trace; outcome data writes back into a JSONL store that powers the `/dashboard` rolling top-hypothesis-accuracy curve.
- **Hosting:** Cloud Run with min-instances=0 for cost discipline. Multi-stage Docker image carrying Python 3.12 and Node 20 (the Dynatrace MCP server is npx-only).
- **Secrets:** Google Secret Manager for the Mongo URI and the Dynatrace OAuth client credentials. The Cloud Run revision pulls secrets at startup, never writes them to disk.

A note on the live URL, since judges read the code: the production wiring is genuinely ADK (the path described above, selected by `_build_production_wiring()` in `app.py`). The public demo URL runs with `DEMO_MODE` set: a deterministic faked replay of that same architecture, chosen for speed, cost, and reliability during judging. The one exception is Slack, which posts for real. To exercise the real ADK + Gemini + MCP path, run the production wiring locally with credentials.

We followed test-driven development from day one. Every module had a failing test before its implementation shipped; the critical-path package is gated at 100% line + 100% branch coverage with mutation testing as a weekly check.

## Challenges we ran into

- **Proving ADK + Dynatrace MCP + Gemini compose, on Day 0.** Before committing to the architecture we ran a Day-0 spike that stood up an ADK `LlmAgent`, attached the Dynatrace MCP server as an `McpToolset` over `StdioConnectionParams`, and ran a Gemini round-trip through the ADK `Runner`. That spike (the `timeout=90` npx launch pattern now in `build_dynatrace_toolset`) is what de-risked the whole build: it told us the three pieces fit before we wrote a line of orchestration logic.
- **Keeping a deterministic core under a non-deterministic runtime.** The hard call was how much decision-making to hand to the LLM planner. Pushing memory routing, hypothesis ranking, and the rejection-replan into the agent would have made the system non-deterministic (breaking the replayable demo) and untestable (breaking the coverage floor). We kept that logic in a pure `Orchestrator` class and made the specialists ADK `FunctionTool`s the agent calls, and the synthesizer prose an ADK `Runner` round-trip. The result is genuinely ADK at the runtime layer and still 100% branch-covered and demo-replayable. That discipline is the reason the docs and the code agree.
- **Dynatrace MCP arg-shape drift.** The MCP server's `execute_dql` tool started rejecting the documented `{"query": ..., "parameters": ...}` envelope mid-build. Re-recording the contract cassettes against the live MCP surfaced the new shape, `{"dqlStatement": ...}`, and a one-line fix landed under a new regression test. The deep-module rule paid off: every specialist call routes through `DynatraceClient.execute_dql`, so the fix was a single file edit, not a five-file refactor.
- **Corporate-network TLS inspection.** The builder's office network MITMs outbound TLS, which blocked `mongo+srv://` SRV lookup during local Atlas validation. Cloud Run egress is clean, so production was unaffected, but the local dev path needed an explicit `tlsAllowInvalidCertificates` knob. Documented as a hygiene item; the production Mongo URI lives in Secret Manager with no such knob.
- **Windows ADK CLI rough edges.** The ADK CLI's `adk deploy` path is unstable on Windows hosts (path-separator and venv-discovery bugs). We worked around it by deploying directly through `gcloud run deploy --source=.` and Cloud Build, which is simpler anyway: fewer moving parts, fewer abstractions to debug.
- **Dynatrace OAuth client for non-interactive runtimes.** Spike work used the MCP's browser-OAuth fallback (cached session in the human's browser). Cloud Run has no browser, so live MCP calls from the deployed container need programmatic OAuth client credentials. The public demo URL ships with `DEMO_MODE` set (in-process faked replay for the demo path, with Slack posting for real); the OAuth client + secret rotation are a one-command flip the moment the credentials exist.
- **50-req/min Dynatrace rate limit.** Not a surprise, it is documented, but enforcing it required sequencing the five specialists instead of fanning out, with rate-limit pacing in the `DynatraceClient` itself. Designed-in, not retrofitted.

## Accomplishments we are proud of

- **Four distinct wow moments** in a single 3-minute demo: cold incident to 90-second ranked brief, live trace UI showing the agent's plan unfold, pre-flight memory short-circuit, and a rolling self-improvement curve.
- **318 tests passing**, 100% line + 100% branch coverage on every critical-path module, gated in CI, with the orchestrator running on a genuine ADK runtime.
- **About $0.21 total cloud spend** across the four-week build (Cloud Run + Atlas M0 + Vertex AI for embeddings + a tiny Cloud Build bill). The 4-tier kill-switch and SHA-keyed LLM caching kept the run cheap.
- **Five specialists, one contract.** Every specialist exposes `investigate(signature)` returning `Evidence` and nothing else, and each is attached to the orchestrator agent as one ADK `FunctionTool`. Adding the sixth would be 30 minutes.
- **A learning loop that survives turnover.** The institutional tribal knowledge becomes structured, queryable, and outlives the senior engineer who knew it.

## What we learned

- **Deep modules pay for themselves under hackathon time pressure.** When the Dynatrace MCP arg shape changed mid-build, the fix touched one file. The naive design, where every specialist composes its own MCP envelope, would have touched five.
- **TDD on agent code is not slower; it is faster.** Every time we changed the orchestrator's pre-flight logic, the unit tests caught a regression in seconds rather than at the next live demo run. The 100% branch coverage gate stopped us from silently introducing dead code paths.
- **"Move beyond chat" is more than a host slogan.** The judges' bar is multi-step execution against real tools. Agent demos that simulate that with prompt-engineering smoke fail in the first thirty seconds of a real run.
- **Pre-flight memory match is the highest-ROI feature for a learning system.** The full 5-specialist pipeline produces excellent briefs, but the 30-second short-circuit on a recognized pattern is the moment the system stops being a "smart triage script" and starts being institutional memory.
- **You can run on ADK without surrendering testability.** The temptation with an agent framework is to let the LLM plan everything. Expressing the system through ADK primitives (`LlmAgent`, `FunctionTool`, `McpToolset`, `Runner`) while keeping the decision logic in a deterministic core gave us both: a real ADK runtime and a 100%-covered, replayable system. The Day-0 spike that proved ADK + Dynatrace MCP + Gemini compose was the single highest-leverage hour of the build.

## What's next for Causal On-Call

The full roadmap lives in [`ROADMAP.md`](ROADMAP.md). Headlines:

- **Plug-and-play observability backends** so the same agent reasons over Datadog or New Relic. The deep-module shape on `DynatraceClient` means the swap is a new module, not a rewrite.
- **Multi-tenant deployment** with per-tenant secret partitioning, memory namespacing, and per-tenant rate-limit accounting on the shared 50/min MCP budget.
- **Per-specialist few-shot pools** where each specialist learns only from resolutions where *its* evidence was decisive. Triage should not learn from cases the Vuln/Sec specialist closed.
- **Negative-evidence memory.** Today's memory records what *was* the root cause. Also record what the agent considered and ruled out, so future investigations skip already-falsified hypotheses faster.
- **Drift detection on the eval metric.** The Phoenix dataset has the data; we just need an alert when the top-hypothesis-accuracy drops more than 10% week-over-week.

## Built With

- google-adk (Agent Development Kit): LlmAgent + FunctionTool + McpToolset + Runner
- gemini-2.5-pro (orchestrator + synthesizer), gemini-3.5-flash (specialists)
- dynatrace-mcp (`@dynatrace-oss/dynatrace-mcp-server`, wired as an ADK McpToolset, the partner-bucket integration)
- mongodb-atlas (operational + vector search)
- vertex-ai (text-embedding-005, 768-dim embeddings)
- arize-phoenix (OSS SDK for tracing + evals)
- google-cloud-run
- google-cloud-secret-manager
- python-3.12, fastapi, pydantic, uvicorn
- pytest, pytest-cov, mutmut, ruff, black
- node-20 (runtime for the npx-distributed MCP server)
- docker (multi-stage build: Python + Node)
- slack-sdk (Block Kit + feedback button)
