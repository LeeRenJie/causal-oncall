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
3. **Otherwise dispatches five specialist sub-agents** — Triage, Topology, Deploy Correlation, Anomaly Window, Vuln/Sec — each scoped to a narrow Dynatrace MCP toolset, sequenced to respect the 50-request-per-minute rate limit.
4. **Aggregates structured evidence** and hands it to a synthesizer that ranks hypotheses deterministically (`0.4·supporting_count + 0.4·mean_confidence + 0.2·specialist_trust`) and uses Gemini only for prose. Every supporting fact carries a clickable link back to the Dynatrace UI.
5. **Delivers the brief** to Slack and as a Dynatrace problem comment via the official MCP `post_problem_comment` tool. One-click feedback from the on-call closes the loop back into the memory store and the Phoenix eval dataset that powers the rolling self-improvement metric.

The agent does not replace the on-call. It gives them, at minute one, the page they used to build by hand at minute fifteen.

## How we built it

- **Reasoning brain:** Gemini 3.1 Pro on Vertex AI for the Orchestrator and Synthesizer; Gemini 3.1 Flash for the five specialist sub-agents to keep per-incident LLM cost near $0.08.
- **Orchestration:** Google Cloud Agent Development Kit (ADK). One Orchestrator dispatches five `Specialist` sub-agents over a single `Specialist.investigate(signature) → Evidence` contract.
- **Partner integration (load-bearing):** the Dynatrace MCP server `@dynatrace-oss/dynatrace-mcp`. Every specialist's only window into Dynatrace is the `DynatraceClient` module, which wraps the MCP, paces requests for the 50/min sliding window, and maps transport errors to domain exceptions. The tools used cover `execute_dql`, `list_problems`, `get_problem_details`, `list_analyzers`, `run_changepoint_analyzer`, `run_forecast_analyzer`, `get_topology_neighbors`, `list_vulnerabilities`, `post_problem_comment`, and `send_event`.
- **Memory:** MongoDB Atlas — vector search (768-dim `text-embedding-005` embeddings) for the pre-flight pattern match, regular operational reads/writes for the incident record store.
- **Observability + evals:** Arize Phoenix OSS SDK. Every agent run produces a trace; outcome data writes back into a JSONL store that powers the `/dashboard` rolling top-hypothesis-accuracy curve.
- **Hosting:** Cloud Run with min-instances=0 for cost discipline. Multi-stage Docker image carrying Python 3.12 and Node 20 (the Dynatrace MCP server is npx-only).
- **Secrets:** Google Secret Manager for the Mongo URI and the Dynatrace OAuth client credentials. The Cloud Run revision pulls secrets at startup, never writes them to disk.

We followed test-driven development from day one. Every module had a failing test before its implementation shipped; the critical-path package is gated at 100% line + 100% branch coverage with mutation testing as a weekly check.

## Challenges we ran into

- **Dynatrace MCP arg-shape drift.** The MCP server's `execute_dql` tool started rejecting the documented `{"query": ..., "parameters": ...}` envelope mid-build. Re-recording the contract cassettes against the live MCP surfaced the new shape — `{"dqlStatement": ...}` — and a one-line fix landed under a new regression test. The deep-module rule paid off: every specialist call routes through `DynatraceClient.execute_dql`, so the fix was a single file edit, not a five-file refactor.
- **Corporate-network TLS inspection.** The builder's office network MITMs outbound TLS, which blocked `mongo+srv://` SRV lookup during local Atlas validation. Cloud Run egress is clean, so production was unaffected, but the local dev path needed an explicit `tlsAllowInvalidCertificates` knob. Documented as a hygiene item; the production Mongo URI lives in Secret Manager with no such knob.
- **Windows ADK CLI rough edges.** The ADK CLI's `adk deploy` path is unstable on Windows hosts (path-separator and venv-discovery bugs). We worked around it by deploying directly through `gcloud run deploy --source=.` and Cloud Build, which is simpler anyway — fewer moving parts, fewer abstractions to debug.
- **Dynatrace OAuth client for non-interactive runtimes.** Spike work used the MCP's browser-OAuth fallback (cached session in the human's browser). Cloud Run has no browser, so live MCP calls from the deployed container need programmatic OAuth client credentials. The W4-S1 deploy ships in `CAUSAL_ONCALL_DEMO_MODE=true` with in-process fakes for the demo path; the OAuth client + secret rotation are a one-command flip the moment the credentials exist.
- **50-req/min Dynatrace rate limit.** Not a surprise — it's documented — but enforcing it required sequencing the five specialists instead of fanning out, with rate-limit pacing in the `DynatraceClient` itself. Designed-in, not retrofitted.

## Accomplishments we are proud of

- **Four distinct wow moments** in a single 3-minute demo: cold incident → 90-second ranked brief, live trace UI showing the agent's plan unfold, pre-flight memory short-circuit, and a rolling self-improvement curve.
- **268 tests passing**, 100% line + 100% branch coverage on every critical-path module, gated in CI.
- **About $0.21 total cloud spend** across the four-week build (Cloud Run + Atlas M0 + Vertex AI for embeddings + a tiny Cloud Build bill). The 4-tier kill-switch and SHA-keyed LLM caching kept the run cheap.
- **Five specialists, one contract.** Every specialist exposes `investigate(signature) → Evidence` and nothing else. Adding the sixth would be 30 minutes.
- **A learning loop that survives turnover.** The institutional tribal knowledge becomes structured, queryable, and outlives the senior engineer who knew it.

## What we learned

- **Deep modules pay for themselves under hackathon time pressure.** When the Dynatrace MCP arg shape changed mid-build, the fix touched one file. The naive design — every specialist composes its own MCP envelope — would have touched five.
- **TDD on agent code is not slower; it is faster.** Every time we changed the orchestrator's pre-flight logic, the unit tests caught a regression in seconds rather than at the next live demo run. The 100% branch coverage gate stopped us from silently introducing dead code paths.
- **"Move beyond chat" is more than a host slogan.** The judges' bar is multi-step execution against real tools. Agent demos that simulate that with prompt-engineering smoke fail in the first thirty seconds of a real run.
- **Pre-flight memory match is the highest-ROI feature for a learning system.** The full 5-specialist pipeline produces excellent briefs, but the 30-second short-circuit on a recognized pattern is the moment the system stops being a "smart triage script" and starts being institutional memory.

## What's next for Causal On-Call

The full roadmap lives in [`ROADMAP.md`](ROADMAP.md). Headlines:

- **Plug-and-play observability backends** so the same agent reasons over Datadog or New Relic. The deep-module shape on `DynatraceClient` means the swap is a new module, not a rewrite.
- **Multi-tenant deployment** with per-tenant secret partitioning, memory namespacing, and per-tenant rate-limit accounting on the shared 50/min MCP budget.
- **Per-specialist few-shot pools** where each specialist learns only from resolutions where *its* evidence was decisive — Triage should not learn from cases the Vuln/Sec specialist closed.
- **Negative-evidence memory.** Today's memory records what *was* the root cause. Also record what the agent considered and ruled out, so future investigations skip already-falsified hypotheses faster.
- **Drift detection on the eval metric.** The Phoenix dataset has the data; we just need an alert when the top-hypothesis-accuracy drops more than 10% week-over-week.

## Built With

- google-cloud-adk (Agent Development Kit)
- gemini-3.1-pro, gemini-3.1-flash (Vertex AI)
- dynatrace-mcp (`@dynatrace-oss/dynatrace-mcp` — the partner-bucket integration)
- mongodb-atlas (operational + vector search)
- arize-phoenix (OSS SDK for tracing + evals)
- google-cloud-run
- google-cloud-secret-manager
- python-3.12, fastapi, pydantic, uvicorn
- pytest, pytest-cov, mutmut, ruff, black
- node-20 (runtime for the npx-distributed MCP server)
- docker (multi-stage build: Python + Node)
- slack-sdk (Block Kit + feedback button)
