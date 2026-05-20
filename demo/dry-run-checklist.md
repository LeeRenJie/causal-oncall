# Pre-recording dry-run checklist

Run this checklist immediately before each take. Three consecutive clean takes are the bar (per PLAN W4-S2 done-means). If any step fails, fix or splice in the wow_backup; do not record a known-broken run.

---

## 10-item pre-flight (do once per recording session, ~5 min)

- [ ] **1. Live URL is up.** `curl -sS -o /dev/null -w "%{http_code}\n" https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true` returns `200`. If `404`/`5xx`, surface to user immediately — Cloud Run revision may have been torn down.
- [ ] **2. Cloud Run is warm.** Hit `/dashboard?demo=true` once 60 seconds before recording. Cold-start adds 5-15s; this is the silent demo-killer.
- [ ] **3. Dashboard data shape is right.** `curl -sS https://causal-oncall-856589756095.us-central1.run.app/dashboard/data?demo=true` returns `rolling_accuracy: 0.73`, `trend` starting at `0.41`, `total_briefs: 147`, `confirmed_count: 107`. If the numbers drift, the narration breaks.
- [ ] **4. Webhook returns a brief in <90s.** Time the curl: `time curl -sS -X POST https://causal-oncall-856589756095.us-central1.run.app/webhook/dynatrace-problem -H "content-type: application/json" -d @tests/fixtures/incidents/payment_latency_spike.json -o /dev/null`. Should land <10s on warm container.
- [ ] **5. Brief shape matches narration.** The response JSON's top hypothesis must be `db_pool_exhaustion` (score ~0.83), top recommendation must contain "Roll back deploy v412". If the model drifts (it shouldn't — demo wiring is deterministic), the narration's specific numbers go stale.
- [ ] **6. Trace SSE stream works.** Open `/trace/-9223372036854775807_v2` in a browser tab. Fire the webhook from another tab. The trace page should populate within 2s with: orchestrator-started, 5x specialist-dispatched + completed pairs, brief-ready. If the SSE stalls, refresh the trace tab and retry — there's a one-shot per problem_id semantic.
- [ ] **7. Browser tabs in order.** Tab 1: dashboard. Tab 2: trace. Tab 3: terminal with curl pre-typed but NOT executed. Tab 4: Dynatrace UI screenshot. Cmd+1/2/3/4 muscle-memory ready.
- [ ] **8. OBS scene is clean.** No personal notification chrome, no Slack badge, no inbox red dot in frame. Browser zoom 110%. OBS recording at 1080p30, mic at -12 dB peak, system audio muted.
- [ ] **9. wow_backups exist.** All four files present in `causal-oncall/demo/wow_backups/`. Re-shoot any that don't match the current dashboard numbers.
- [ ] **10. Timer ready.** Phone stopwatch or OBS timer overlay. The 3:00 cap is real — judges stop watching at 3:05 in our experience.

---

## 3x clean-run discipline

Per PLAN W4-S2: the demo must run cleanly three times in a row before the final take.

Track here:

| Take | Date/time | Cold-start time (s) | Webhook latency (s) | Brief top hypothesis | Brief score | Trace SSE complete? | Dashboard data correct? | Clean? |
|---|---|---|---|---|---|---|---|---|
| Take 1 |  |  |  |  |  |  |  |  |
| Take 2 |  |  |  |  |  |  |  |  |
| Take 3 |  |  |  |  |  |  |  |  |

"Clean" = no UI stutter, no off-script narration, no visible cursor fumble, runtime within 2:55-3:05.

If Take N is not clean, restart from Take 1. (Two clean + one flake means re-shoot, not splice.)

---

## Common failures + remediation

| Symptom | Cause | Remediation |
|---|---|---|
| Webhook returns 5xx | Cloud Run revision crashed | Check `gcloud run services logs read causal-oncall --region us-central1 --limit=20`; redeploy if needed |
| Webhook returns 200 but `top_hypothesis_key != "db_pool_exhaustion"` | Demo wiring changed shape | Open `src/causal_oncall/_demo_wiring.py`, re-pin the stub_dql tags |
| Trace SSE never receives `brief-ready` | Webhook fired before trace tab opened, or trace tab opened after brief was already complete | Refresh trace tab BEFORE firing webhook; the SSE stream is per-problem-id and replays the buffered events |
| Dashboard sparkline is empty | `?demo=true` query string dropped | Re-paste the full URL; the demo mode is gated on the literal query string |
| Cold start > 15s | min-instances=0 (default) | Per PLAN R6: temporarily `gcloud run services update causal-oncall --min-instances=1 --region us-central1` for the recording window. Cost +$0.50/day, well within budget. Revert after submission. |
| OBS audio out of sync | Recording at variable framerate | OBS settings to constant 30fps; re-record |

---

## Submission-day go/no-go

Final pre-submission check (Friday morning, before clicking Submit):

- [ ] Three clean takes are stored locally
- [ ] Best take is uploaded to YouTube as **unlisted** (not private, not public)
- [ ] YouTube auto-captioning has been reviewed; no embarrassing transcription errors in the first 30s
- [ ] Video runtime is 2:55-3:05
- [ ] First frame is the Dynatrace problem screenshot (no OBS countdown leftover)
- [ ] Last 2s are a still frame of the dashboard with the 73% number visible (so the thumbnail picks up the wow)
