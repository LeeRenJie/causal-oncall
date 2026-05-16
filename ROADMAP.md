# Roadmap

Post-hackathon expansion. Phases are ordered by dependency, not by calendar.
Effort estimates assume one full-time engineer-week unless noted.

## Phase 1 — Harden the learning loop (4–6 weeks)

The hackathon ships the five learning-loop mechanisms in their simplest
form. Each can be broken down further:

### 1.1 Embed-then-cluster pattern miner (2 weeks)
The Curator promotes few-shot examples by `confirmed_root_cause_key`
equality today. Replace with a semantic clustering pass over the
embedding space so visually-distinct labels that describe the same
underlying failure mode cluster together.

### 1.2 Per-specialist few-shot pools (1 week)
Today's Curator writes one shared few-shot bank. Split it so each
specialist learns from the resolutions where *its* evidence was
decisive. Triage should not learn from incidents where the
Vuln/Sec specialist closed the case.

### 1.3 Active-learning gap detection (2 weeks)
Surface incidents where the agent's confidence was low but the
on-call's verdict was clear. Promote those to a "fix me first"
queue for the maintainer.

### 1.4 Negative-evidence memory (1 week)
Today's memory records what *was* the root cause. Also record what
the agent considered and ruled out, so future investigations skip
already-falsified hypotheses faster.

### 1.5 Drift detection on the eval metric (1 week)
The Phoenix eval dataset has the data; we just need an alert when
top-hypothesis-accuracy drops more than 10% week-over-week.

## Phase 2 — Multi-tenant deployment (6 weeks)

The hackathon entry is single-tenant. Multi-tenant requires:

- Per-tenant secret partitioning in Secret Manager
- Per-tenant memory store namespaces in Atlas
- Per-tenant rate-limit accounting on the Dynatrace MCP server
  (their 50 req/min is per tenant, but our orchestrator does not
  yet know that the budget is shared across customers if we deploy
  to one shared Cloud Run instance)
- Tenant-scoped Phoenix projects so eval datasets do not leak across customers

The cleanest path is one Cloud Run service per tenant, sharing a
control-plane that handles provisioning. Cost remains negligible at
the hackathon's incident volume.

## Phase 3 — Customer-specific specialist fine-tuning (4 weeks)

Once a customer accumulates 50+ resolved incidents, we can fine-tune
the specialist prompts on their data. Two paths:

1. **Few-shot at runtime** — keep base model, dynamically inject the
   five most-similar resolved incidents as few-shots. Cheaper, no
   infra. Already partially implemented via Curator.
2. **LoRA fine-tunes per specialist** — Vertex AI supports LoRA on
   Gemini 3 Flash. Higher up-front cost (~$50 per fine-tune), but
   removes the per-call token cost of inline few-shots.

Decision deferred until we have at least one customer with the
volume to justify it.

## Phase 4 — Real-time topology graph diffing (3 weeks)

The Topology specialist today queries the current graph. The
under-investigated failure mode is "the graph itself changed" —
a new dependency edge appeared the moment before the incident,
e.g. a service started calling a database it had never called before.
Compute the diff between graph snapshots taken at fixed intervals
and surface as a new evidence kind.

This is novel enough that it could be a follow-up demo on its own.

## Phase 5 — Plug-and-play replacements for non-Dynatrace observability (8 weeks each)

The specialist contract is observability-agnostic; the
`DynatraceClient` is the only Dynatrace-specific module. Implement
parallel clients to demonstrate:

- **DatadogClient** — uses Datadog's REST API. Same `execute_query`
  / `get_problem_context` / `get_topology_neighbors` shape.
  Their query language (Datadog metrics syntax + log search) needs
  a parallel specialist-side prompt set.
- **NewRelicClient** — uses NRQL. Mostly a query-language remap;
  the topology API is materially weaker than Dynatrace's, so the
  Topology specialist degrades to "service-list with health" rather
  than true graph traversal.
- **OpenTelemetry-native client** — for customers running their own
  Tempo/Loki/Mimir stack. The contract holds; the implementation
  is "stitch together three OSS APIs".

Each adds roughly 8 weeks. The win is that the agent stops being a
Dynatrace-specific tool and becomes "the SRE pre-mortem agent for
*your* observability stack".

## Phase 6 — Closed-loop remediation (gated on customer demand)

Today the agent recommends; the on-call acts. The natural extension
is to let the agent execute the highest-confidence action when the
memory match exceeds a very high threshold (≥0.98) and the proposed
fix is reversible (e.g. roll back a deploy that the agent itself
identified as the trigger 14 times before).

Risks:
- Closed-loop actions need their own kill switch — a deploy-rollback
  webhook with a two-person-rule confirmation, not a one-LLM-call
  trigger.
- Customer trust must be earned through the hypothesis-accuracy
  metric first. We do not ship closed-loop remediation until that
  metric stays above 80% for three consecutive months on the
  candidate customer's tenant.

Not committed. Listed for completeness.
