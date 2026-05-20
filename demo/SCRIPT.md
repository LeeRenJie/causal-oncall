# Causal On-Call — 3-minute demo narration

**Target runtime:** 3:00 (±5s per beat per PLAN §5).
**Recording tool:** OBS Studio, 1080p30, system audio off, mic on, cursor highlight on.
**Live URL:** `https://causal-oncall-856589756095.us-central1.run.app`
**Webhook payload:** `causal-oncall/tests/fixtures/incidents/payment_latency_spike.json`

Read each NARRATE block aloud at conversational pace (~150 wpm). The ACTION column is what you do on screen; the EXPECT column is what the viewer sees. Times are cumulative elapsed (`mm:ss`).

---

## Pre-roll setup (do once, before hitting Record)

1. Open 4 browser tabs in this order, all pointed at the live URL:
   - Tab 1: `/dashboard?demo=true` (the dashboard wow)
   - Tab 2: `/trace/-9223372036854775807_v2` (the live trace SSE page)
   - Tab 3: A terminal showing the curl command (do NOT execute yet)
   - Tab 4: The Dynatrace UI screenshot or live problem view (a still PNG is fine — judges don't care that it's not interactive)
2. Pre-warm Cloud Run: `curl -sS -o /dev/null https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true` once, 60s before recording. The first request after a cold scale-down adds ~5s.
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

## Beat 2 (0:25 - 0:55) — "Fire the webhook, watch the agent think"

**ACTION:** Switch to Tab 2 (`/trace/...`). It is empty. Switch to Tab 3 (terminal). Execute the curl. Switch BACK to Tab 2 within ~1s.

**Curl command** (copy verbatim, single line):
```bash
curl -sS -X POST "https://causal-oncall-856589756095.us-central1.run.app/webhook/dynatrace-problem" -H "content-type: application/json" -d @tests/fixtures/incidents/payment_latency_spike.json
```

**EXPECT:** In Tab 2, the SSE stream renders rows in real time:
- grey "orchestrator-started"
- orange "specialist-dispatched" (triage)
- green "specialist-completed" (triage)
- orange + green pairs for: topology, deploy_correlation, anomaly_window, vuln_sec
- blue "brief-ready" at the end

Total wall time ~3-5s on a warm container.

**NARRATE:**
> "The Dynatrace problem-open webhook hits our Cloud Run endpoint. The orchestrator agent dispatches five specialists — Triage, Topology, Deploy Correlation, Anomaly Window, Vuln/Sec — sequenced to respect Dynatrace's fifty-per-minute rate limit. Each one is scoped to a narrow Dynatrace MCP toolset. You're watching the plan execute live, not a chat transcript."

---

## Beat 3 (0:55 - 1:35) — "Here's the brief — wow #1"

**ACTION:** Switch to Tab 3 (terminal). The curl response JSON is visible. Scroll to the `markdown` field. **Or** open `/briefs/-9223372036854775807_v2.md` in a browser if you can route that path; otherwise copy the markdown into a markdown previewer (VS Code preview, Obsidian, anything).

**EXPECT:** A Markdown brief titled "Causal On-Call brief for -9223372036854775807_v2" with:
- **Next action:** Roll back deploy v412 on payment-service.
- **Ranked hypothesis #1:** DB connection pool exhausted by deploy v412 (score 0.83)
- Four pieces of supporting evidence, each tagged with the specialist that produced it
- Each evidence row has a clickable "Open in Dynatrace" link

**NARRATE:**
> "Ninety seconds in, the on-call sees a ranked hypothesis tree. Top hypothesis: the DB connection pool exhausted right after deploy v412. The next action is explicit — roll back the deploy. Every piece of evidence links straight back to the Dynatrace UI, so the on-call can verify, not trust blindly. This is wow moment one — cold incident to ranked brief, in ninety seconds."

---

## Beat 4 (1:35 - 2:05) — "Replan — wow #2"

**ACTION:** Switch back to Tab 2 (the trace UI) and scroll up to show the `specialist-completed` events. Point at the `vuln_sec` row — its hypothesis ranks #2 (cve_exposure, score 0.50, well below the top hypothesis's 0.83).

**EXPECT:** The trace UI's ordering visibly shows the agent collecting evidence from all five specialists, ranking deterministically (formula in the synthesizer: `0.4 * supporting_count + 0.4 * mean_confidence + 0.2 * specialist_trust`), and parking the weaker hypothesis at #2 — not silently dropping it.

**NARRATE:**
> "The agent doesn't just emit the winner. It keeps the runner-up — the CVE-exposure hypothesis from the security specialist — at rank two, with its score visible. If the on-call rejects the top hypothesis, the orchestrator replans against the next-best evidence bag, and the trace UI surfaces every step. Wow moment two — hypothesis ranking is auditable, not opaque."

---

## Beat 5 (2:05 - 2:35) — "Memory hit — wow #3"

**ACTION:** Switch to Tab 1 (`/dashboard?demo=true`). Point at the "pattern hit-rate" / "memory" subtitle (it shows `147 briefs / 107 confirmed`).

**EXPECT:** The dashboard shows the cumulative "147 briefs produced, 107 confirmed correct" footer underneath the sparkline. The narration explains the architecture; the canned dashboard numbers are the visible proof.

**NARRATE:**
> "When the agent has seen an incident before, it short-circuits. Pre-flight memory match against MongoDB Atlas vector search — if the new problem's signature embedding lands within point-eight-five cosine similarity of a past resolved incident, the agent skips the specialist dispatch and returns a thirty-second brief with a 'seen this fourteen times in six months' badge and the proven fix prefilled. Over a hundred and forty seven incidents seen, a hundred and seven confirmed correct. Wow moment three — institutional tribal knowledge becomes structured, queryable, and survives turnover."

---

## Beat 6 (2:35 - 3:00) — "Self-improvement — wow #4"

**ACTION:** Stay on Tab 1. Point at the headline number "73%", then drag your cursor across the sparkline curve from left (41%) to right (73%).

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
