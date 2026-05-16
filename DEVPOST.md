# Causal On-Call — Devpost Submission

**Track:** Dynatrace

## Inspiration

Every on-call engineer we have talked to describes the same first fifteen minutes: a frantic scavenger hunt across dashboards, logs, deploy history, and Slack threads. The information needed to triage the incident is *already in Dynatrace* — it just takes a senior SRE's pattern matching to pull the right threads in the right order. We wanted to give every on-call engineer that senior SRE in their pocket, so the first page they see is the one they would have built by hand at minute fifteen.

## What it does

When a Dynatrace problem opens, Causal On-Call:

1. Normalizes the problem into a stable signature.
2. Queries an incident memory store for a high-confidence match against past resolved incidents. On a hit, it short-circuits the investigation and produces a brief in roughly 30 seconds with a "seen this 14 times in 6 months" badge plus the proven fix.
3. Otherwise, dispatches five specialist sub-agents (Triage, Topology, Deploy Correlation, Anomaly Window, Vuln/Sec), each scoped to a narrow Dynatrace toolset.
4. Aggregates their structured evidence and hands it to a synthesizer that ranks hypotheses deterministically and drafts prose via Gemini.
5. Posts the brief to Slack and as a Dynatrace problem comment, with a one-click feedback loop that closes the learning loop back into the memory store.

The agent does not replace the on-call. It gives them the page they used to build by hand at minute fifteen, at minute one.

## How we built it

- **Reasoning brain:** Gemini 3.1 Pro on Vertex AI, with Gemini 3 Flash for the specialist sub-agents to keep per-incident cost near $0.08.
- **Orchestration:** Google Cloud Agent Development Kit (ADK). One Orchestrator agent dispatches five `Specialist` sub-agents over the shared `Specialist.investigate()` contract.
- **Partner integration (load-bearing):** Dynatrace MCP server (`@dynatrace-oss/dynatrace-mcp`). Every specialist's only window into Dynatrace is the `DynatraceClient` module, which wraps the MCP server, paces requests for the 50-per-minute rate limit, and maps transport errors to domain exceptions.
- **Memory:** MongoDB Atlas — vector search for the pre-flight pattern match, regular operational reads/writes for the incident record store.
- **Observability + evals:** Arize Phoenix OSS SDK. Every agent run produces a trace; on-call feedback writes back into a Phoenix eval dataset that powers the rolling top-hypothesis-accuracy metric.
- **Hosting:** Cloud Run. Multi-stage Docker image carrying Python 3.12 and Node 20 (the MCP server is npx-only).

We followed test-driven development from day one. Every module has a failing-test scaffold before the implementation ships; the critical-path package is gated at 100% line and 100% branch coverage with mutation testing weekly.

## Challenges we ran into

- Wiring three preview surfaces (Gemini 3.1 Pro, ADK, Dynatrace MCP) on day zero meant accepting that any one of them could change shape mid-build. The Day-0 spike kit let us trim risk before committing to the 4-week plan.
- The Dynatrace SaaS API enforces a 50-requests-per-minute sliding-window rate limit per tenant. We sequenced the specialists rather than fanning them out in parallel and built rate-limit pacing into the `DynatraceClient` from the first commit, not as a retrofit.
- Atlas M0 free-tier vector indexes have to be hand-created in the UI. We documented the exact index spec in the README and verified it in the spike before depending on it.

## Accomplishments we are proud of

- Five specialist sub-agents, all sharing one contract, no copy-paste.
- 100% line and branch coverage on the critical-path package, enforced in CI.
- A pre-flight memory match that turns 90-second briefs into 30-second briefs when the agent has seen the pattern before — the kind of compounding speedup that justifies the system over time.
- A learning loop that survives turnover: the institutional tribal knowledge ends up structured, queryable, and trustworthy, not stuck in a slack DM with a senior engineer who left.

## What we learned

- Deep modules pay for themselves under hackathon time pressure. The narrow public interface on `DynatraceClient` let us swap the MCP transport twice during the build without touching a single specialist.
- TDD on agent code is not slower; it is faster. Every time we changed the orchestrator's pre-flight logic, the unit tests caught a regression in seconds rather than at the next live demo run.
- "Move beyond chat" is more than a slogan. The judges' bar is multi-step execution against real tools; agent demos that simulate that with prompt-engineering smoke fail in the first 30 seconds of a real demo.

## What's next for Causal On-Call

The full roadmap lives in `ROADMAP.md`. The headline items:

- Plug-and-play observability backends so the same agent reasons over Datadog or New Relic.
- Multi-tenant deployment so multiple SRE orgs share one fleet of agents without sharing memory.
- Customer-specific fine-tuning of the specialists from each org's resolved-incident history.
- Real-time topology graph diffing — alert on the upstream change, not just its downstream symptom.

## Built With

- Google Cloud Agent Development Kit (ADK)
- Gemini 3.1 Pro on Vertex AI
- Dynatrace MCP server (`@dynatrace-oss/dynatrace-mcp`)
- MongoDB Atlas (operational + vector search)
- Arize Phoenix (OSS observability SDK)
- Cloud Run
- Python 3.12, FastAPI, Pydantic
- pytest, pytest-cov, mutmut
- Docker
