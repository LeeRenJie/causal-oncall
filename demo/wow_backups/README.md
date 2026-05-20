# Wow backups — capture instructions

**Decision:** Manual screenshot/screen-recording rather than Playwright automation.
**Why:** Playwright is not installed in the local Windows toolchain. The install (pip + `playwright install chromium`, ~300 MB) plus the per-browser auth-prompt debug loop on a Windows sandbox typically takes 30-60 minutes. The dry-run takes already produce these screenshots organically; capturing them during the 3x dry-run is faster than scripting them.
**Captured during:** Dry-run takes (each take produces one of each backup naturally).
**Status:** Empty until the user does the dry-run.

---

## File manifest (target state — populate during dry-run)

| File | What it captures | When in the demo | Purpose |
|---|---|---|---|
| `wow1_cold_incident_brief.png` | The terminal showing curl response with ranked brief markdown | Beat 3 (0:55-1:35) | Splice if live curl returns 5xx |
| `wow1_cold_incident_brief.mp4` | Screen-recording from curl execution to brief render (~10s) | Beat 3 | Splice if live demo flakes |
| `wow2_hypothesis_rejection.png` | Trace UI SSE stream fully populated with all 5 specialists + brief-ready | Beat 4 (1:35-2:05) | Splice if trace SSE stalls |
| `wow2_hypothesis_rejection.mp4` | Screen-recording of trace tab filling in as specialists complete (~5s) | Beat 4 | Splice if SSE doesn't replay |
| `wow3_memory_match_short_circuit.png` | Dashboard `?demo=true` zoomed on the "147 briefs / 107 confirmed" subtitle | Beat 5 (2:05-2:35) | Splice always (no live path) |
| `wow4_dashboard_curve.png` | Dashboard `?demo=true` full view, headline 73% + sparkline | Beat 6 (2:35-3:00) | Splice if dashboard renders empty |
| `wow4_dashboard_curve.mp4` | Screen-recording from page load to sparkline render (~3s) | Beat 6 | Splice if dashboard JS errors |

---

## How to capture each (Windows-native, no extra tooling)

### Static screenshots

Use Windows Snipping Tool (`Win+Shift+S`):
1. Window snip → click the browser/terminal window
2. Save as PNG into this folder with the exact filename above

For terminal screenshots (wow1), use Windows Terminal's Edit > Mark and selection, then `Print Screen` → paste into Paint → crop → Save As PNG.

Resolution target: 1920x1080 or 2560x1440. Larger is fine; we only display embedded in the README and pull into OBS in post.

### Screen recordings (mp4)

Use OBS Studio (already required for the main demo recording):
1. New Scene named `wow_backup_<n>`
2. Source: Display Capture or Window Capture targeting the browser tab
3. Settings > Output: Recording Format = MP4, Encoder = x264 (or NVENC if available), Rate Control = CBR, Bitrate = 8000 Kbps
4. Hit Start Recording, perform the action, Stop Recording
5. Trim with OBS's clip editor or open in any video editor and trim head/tail dead time

Target file size: <20 MB each (Devpost host video size matters less, but the README embeds use proxy paths).

---

## Per-wow capture script (read aloud is optional)

### wow1: cold_incident_brief

```
1. Open terminal.
2. Hit Record.
3. Run:
   curl -sS -X POST https://causal-oncall-856589756095.us-central1.run.app/webhook/dynatrace-problem \
     -H "content-type: application/json" \
     -d @tests/fixtures/incidents/payment_latency_spike.json | python -m json.tool
4. Wait for response.
5. Scroll to show the `markdown` field and `top_recommendation: "Roll back deploy v412..."`.
6. Stop Record.
```

### wow2: hypothesis_rejection (live trace)

```
1. Open browser tab to https://causal-oncall-856589756095.us-central1.run.app/trace/-9223372036854775807_v2
   (the trace page renders empty until a webhook for that problem_id arrives)
2. In a separate terminal, queue the curl from wow1 but don't hit enter.
3. Hit Record on OBS.
4. Bring the trace tab to front.
5. Switch to terminal, hit Enter on the curl, switch back to trace tab within 1s.
6. Watch rows render: orchestrator-started, specialist-dispatched (triage), specialist-completed, ... brief-ready.
7. Stop Record after brief-ready row appears.
```

### wow3: memory_match_short_circuit (static — no live path in demo mode)

```
1. Open browser tab to https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true
2. Wait for sparkline to render.
3. Zoom in on the "147 briefs / 107 confirmed" caption (Ctrl+Plus 2-3 times).
4. Win+Shift+S, select the relevant area.
5. Save as wow3_memory_match_short_circuit.png
```

### wow4: dashboard_curve

```
1. Open browser tab to https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true (hard refresh: Ctrl+Shift+R)
2. Hit Record.
3. Watch the sparkline draw (~100ms — recording captures the steady state).
4. Stop Record after 3s.
5. For the .png: Win+Shift+S, full-window snip.
```

---

## Quality bar before declaring done

- [ ] All 4 PNGs present and readable at 100% zoom
- [ ] All 3 MP4s present (wow3 is static-only, no video)
- [ ] No personal notification chrome (Slack badge, email count) visible
- [ ] No mouse pointer mid-animation in static PNGs (let cursor settle)
- [ ] Each MP4 <30s, <20MB
- [ ] File names match the manifest above EXACTLY (the SCRIPT.md cut-points reference these literal names)
