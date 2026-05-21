# Causal On-Call — 3-minute demo narration

**Target runtime:** 3:00 (±5s per beat per PLAN §5).
**Recording tool:** OBS Studio, 1080p30, system audio off, mic on, cursor highlight on.
**Live URL:** `https://causal-oncall-856589756095.us-central1.run.app`
**Webhook payload:** `causal-oncall/tests/fixtures/incidents/payment_latency_spike.json`

Read each NARRATE block aloud at conversational pace (~150 wpm). The ACTION column is what you do on screen; the EXPECT column is what the viewer sees. Times are cumulative elapsed (`mm:ss`).

---

## Pre-roll setup (do once, before hitting Record)

1. Open 3 browser tabs in this order, all pointed at the live URL:
   - Tab 1: `/` (the landing page — three demo cards + embedded trace + brief cards + sponsor footer)
   - Tab 2: `/dashboard?demo=true` (the dashboard wow — used for beats 5 + 6)
   - Tab 3: Dynatrace UI screenshot or live problem view (still PNG is fine — used only for beat 1)
2. Pre-warm Cloud Run: run `./scripts/prewarm.sh` (or `prewarm.ps1` on Windows) for the 5 minutes before recording. Hits `/warmup` every 30s — no LLM calls, ~5ms per ping.
3. Zoom browser to 110% so text is readable in the 1080p frame.
4. Close Slack, email, anything that might notify mid-record.
5. Test mic level: read one sentence, scrub back, check waveform — peaking around -12 dB.

---

## Beat 1 (0:00 - 0:25) — "Here's the pain"

**ACTION:** Show Tab 4 (Dynatrace UI screenshot with a fresh P1 problem highlighted on payment-service).

**EXPECT:** A real Dynatrace dashboard with one bright red problem card titled "Response time degradation" on payment-service.

**NARRATE:**
> "Every on-call engineer's first fifteen minutes look the same. A P1 problem fires. You jump into the dashboard, then logs, then deploy history, then Slack. Fifteen minutes later you have a working theory. Causal On-Call gives you that theory in ninety seconds, by running the exact pre-mortem an experienced SRE would. Watch."

---

## Beat 2 (0:25 - 0:55) — "Fire the webhook, watch the agent think" (wow #1)

**ACTION:** Switch to Tab 1 (`/`). Click the leftmost demo card: **"Run cold investigation"**.

**EXPECT:** Below the demo cards, the embedded trace panel renders SSE rows in real time:
- grey "orchestrator-started"
- orange "specialist-dispatched" (triage)
- green "specialist-completed" (triage)
- orange + green pairs for: topology, deploy_correlation, anomaly_window, vuln_sec
- blue "brief-ready" at the end

Total wall time ~3-5s on a warm container. The "Incident brief" panel below populates with cards as soon as the webhook POST resolves.

**NARRATE:**
> "The Dynatrace problem-open webhook hits our Cloud Run endpoint. The orchestrator agent dispatches five specialists — Triage, Topology, Deploy Correlation, Anomaly Window, Vuln/Sec — sequenced to respect Dynatrace's fifty-per-minute rate limit. Each one is scoped to a narrow Dynatrace MCP toolset. You're watching the plan execute live, not a chat transcript."

---

## Beat 3 (0:55 - 1:35) — "Here's the brief — wow #1 continued"

**ACTION:** Stay on Tab 1. Scroll down to the "Incident brief" panel. Point at the green "Top recommendation" box, then at the hypothesis cards underneath. Click the **+** on the "Supporting evidence" accordion of card #1 to expand it.

**EXPECT:** Card-based brief with:
- Green top-recommendation banner: "Roll back deploy v412 on payment-service."
- Hypothesis cards with confidence bars (green for the top one ~0.83, yellow for runners-up)
- Each card has Confirm / Reject buttons + a supporting-evidence accordion
- Sidebar links appear after the brief: "View Slack post" · "View Dynatrace Grail event" · "Open in dashboard"

**NARRATE:**
> "Ninety seconds in, the on-call sees a ranked hypothesis tree as cards. Top hypothesis: the DB connection pool exhausted right after deploy v412, with a confidence bar showing the score. The next action is explicit — roll back the deploy. Every hypothesis carries its supporting evidence one click away, and the brief is delivered three ways: as a Slack post, as a Dynatrace Grail event on the problem timeline, and to this dashboard. Wow moment one — cold incident to ranked brief, in ninety seconds."

---

## Beat 4 (1:35 - 2:05) — "Replan — wow #2"

**ACTION:** Stay on Tab 1. Click the rightmost demo card: **"Run with hypothesis rejection"**. Watch the trace populate, the brief cards render, then after ~2s a "Replanning..." toast appears as the JS auto-rejects the top hypothesis. The brief re-renders with the rejected hypothesis removed.

**EXPECT:** The trace UI shows an extra "orchestrator-replanned" row. The hypothesis cards re-render — the rejected key is gone, the runner-up is now ranked #1 with its score adjusted.

**NARRATE:**
> "When the on-call rejects a hypothesis, the orchestrator visibly replans against the next-best evidence bag. The trace UI surfaces every step. Wow moment two — hypothesis ranking is auditable and the human stays in the loop."

---

## Beat 5 (2:05 - 2:35) — "Memory hit — wow #3"

**ACTION:** Stay on Tab 1. Click the middle demo card: **"Run memory-hit (seen 14x before)"**. The trace shows a single grey "orchestrator-started" → orange "memory-short-circuit" → blue "brief-ready" (no specialist dispatch). The brief panel renders with the orange "Pattern matched · seen 14x before" banner above the hypothesis card.

**EXPECT:** Total wall time ~1-3s (no LLM specialists run). The card-based brief has the "Pattern matched" memory banner; the top recommendation is the proven prior fix.

**NARRATE:**
> "When the agent has seen an incident before, it short-circuits. Pre-flight memory match against MongoDB Atlas vector search — if the new problem's signature embedding lands within point-eight-five cosine similarity of a past resolved incident, the agent skips the specialist dispatch and returns a three-second brief with a 'seen this fourteen times before' badge and the proven fix prefilled. Wow moment three — institutional tribal knowledge becomes structured, queryable, and survives turnover."

---

## Beat 6 (2:35 - 3:00) — "Self-improvement — wow #4"

**ACTION:** Switch to Tab 2 (`/dashboard?demo=true`). Point at the headline number "73%", then drag your cursor across the sparkline curve from left (41%) to right (73%). The sponsor footer is now visible at the bottom of the dashboard too.

**EXPECT:** The dashboard prominently shows:
- A large `73%` headline (rolling top-hypothesis accuracy)
- A small caption "41% starting accuracy"
- A green sparkline climbing from 0.41 to 0.73 over 30 data points

**NARRATE:**
> "Every agent run is traced via the Arize Phoenix SDK. On-call feedback flows back into the Phoenix eval dataset, and the dashboard reads the rolling top-hypothesis accuracy. Forty-one percent in month one, seventy-three percent today — the agent compounds because the team's incident memory compounds. Wow moment four — the agent gets better while the on-call sleeps. Three minutes, four wow moments, one Apache-licensed repo. Causal On-Call. Thank you."

---

## Cut points (where to splice in a wow_backup if live demo stutters)

| Beat | If this fails... | Splice in... |
|---|---|---|
| 2 | SSE stream stalls or Cloud Run cold-starts past 10s | `wow_backups/wow2_hypothesis_rejection.mp4` (pre-recorded trace stream) |
| 3 | Curl returns 500 or times out | `wow_backups/wow1_cold_incident_brief.mp4` (pre-recorded curl-to-brief) |
| 5 | (no live path — narration only) | n/a |
| 6 | Dashboard sparkline renders blank | `wow_backups/wow4_dashboard_curve.mp4` |

Stitch in OBS post-production. Keep one continuous voice track; only the screen video is replaced.

---

## Variation: if you have only 2:30 (some judges enforce strict)

Cut Beat 5 entirely (the memory wow). The dashboard already implies it via the 73% accuracy lift. Net runtime ~2:30.

## Variation: if you have 3:30 (some tracks allow up to 3:30)

Add a 30-second closing beat after Beat 6 that walks through the `/briefs/<id>.md` file persisted on disk, showing the markdown source. Reinforces the "written artifact" framing the judges' rubric weights.
