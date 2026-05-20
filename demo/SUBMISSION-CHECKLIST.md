# Final submission checklist

**Deadline:** 2026-06-12 @ 05:00 GMT+8.
**Submit by:** 2026-06-12 @ 04:30 GMT+8 (30-minute buffer per PLAN W4-S4).
**Devpost form:** https://rapid-agent.devpost.com/

Work top-to-bottom. Anything unchecked at T-1h is a blocker.

---

## T-48h: code + repo polish (Wed evening)

- [ ] All tests green: `cd causal-oncall && .venv/Scripts/python.exe -m pytest -q` reports `268 passing, 100/100` (or higher if new tests landed)
- [ ] Coverage gate: `pytest --cov-branch --cov-fail-under=100` passes
- [ ] Lint clean: `ruff check src tests scripts` + `black --check src tests scripts` both clean
- [ ] No `# pragma: no cover` added without a one-line inline justification
- [ ] BUILD-LOG.md updated with W4-S2 entry (this slice)
- [ ] No secrets in repo: `grep -r "DT_OAUTH\|MONGODB_URI\|SLACK_BOT_TOKEN\|GEMINI_API_KEY" causal-oncall/src causal-oncall/tests` returns nothing (a `.env.example` with placeholders is fine; real values must live in `.env` which is gitignored)

## T-24h: video + assets (Thu evening)

- [ ] 3x clean dry-runs logged in `demo/dry-run-checklist.md`
- [ ] Best take exported as MP4, 1080p, <100 MB (Devpost soft limit)
- [ ] Video uploaded to YouTube as **unlisted** (not private — judges need link access; not public — avoids drive-by views before judging)
- [ ] YouTube auto-captions reviewed; first 30s scrubbed for embarrassing transcription errors
- [ ] Video URL copied for the Devpost form
- [ ] All 4 wow_backups present in `causal-oncall/demo/wow_backups/` (per `demo/wow_backups/README.md` manifest)
- [ ] Demo SCRIPT.md final wording locked (no `[FIXME]` markers)

## T-12h: live URL go/no-go (Fri morning, ~5-6 AM)

- [ ] `curl -sS -o /dev/null -w "%{http_code}\n" https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true` returns `200`
- [ ] Open the URL in an **incognito window** (no auth needed). Dashboard renders 73% headline + sparkline within 3s.
- [ ] Webhook smoke: curl returns 200 with `top_recommendation` containing "Roll back deploy v412"
- [ ] Trace SSE smoke: open `/trace/-9223372036854775807_v2`, fire webhook from another tab, all 5 specialists render
- [ ] If cold-start > 10s: flip to `--min-instances=1` via `gcloud run services update causal-oncall --min-instances=1 --region us-central1` (revert after submission to avoid stray cost)

## T-6h: GitHub repo flip + license

- [ ] Repo flipped from private to public: `gh repo edit LeeRenJie/causal-oncall --visibility public --accept-visibility-change-consequences`
- [ ] GitHub auto-detects the license: visit `https://github.com/LeeRenJie/causal-oncall` and confirm the **About sidebar shows "Apache-2.0"** (Devpost requires a *detectable* OSS license — the LICENSE file alone is not enough until GitHub's license-detector parses it; first push to public main usually takes <60s for detection to update)
- [ ] README.md renders cleanly on GitHub (no broken image links, no relative paths breaking)
- [ ] Final commits pushed to `main`: `git status` returns "working tree clean"
- [ ] **No Claude attribution anywhere** — `grep -r "Co-Authored-By: Claude\|claude\.ai\|🤖" causal-oncall` returns nothing (per user constraint)

## T-2h: Devpost form

- [ ] Open https://rapid-agent.devpost.com/ → "Submit project"
- [ ] **Project name:** Causal On-Call
- [ ] **Tagline (140 char):** "Turn a Dynatrace problem into a ranked SRE incident brief in 90 seconds. Multi-agent ADK + Dynatrace MCP + a memory that compounds."
- [ ] **Track:** Dynatrace (single select — do NOT pick multiple)
- [ ] **Hosted URL:** `https://causal-oncall-856589756095.us-central1.run.app/dashboard?demo=true` (give the dashboard as the landing; judges can pivot to the webhook + trace UI from the README)
- [ ] **GitHub repo URL:** `https://github.com/LeeRenJie/causal-oncall`
- [ ] **Demo video URL:** YouTube unlisted link from T-24h
- [ ] **Project story:** paste body from `causal-oncall/DEVPOST.md` (Inspiration, What it does, How we built it, Challenges, Accomplishments, What we learned, What's next, Built With)
- [ ] **Built With tags:** add each chip individually — `gemini`, `google-cloud-adk`, `dynatrace-mcp`, `mongodb-atlas`, `arize-phoenix`, `cloud-run`, `python`, `fastapi`, `docker`
- [ ] **Try it out links:** add the live URL + the GitHub URL again under "Try it out" section
- [ ] **Image upload:** screenshot of `wow4_dashboard_curve.png` as the cover image (the 73% headline is the strongest hook for the listing thumbnail)

## T-30min: final confirm

- [ ] Open the submitted listing in **incognito** — confirm every link works for an un-authed viewer
- [ ] Hit Submit (it's a separate button from Save in Devpost — Save keeps it as draft)
- [ ] Confirmation email arrives within 60s — screenshot it to `causal-oncall/submission/confirmation.png` (create the `submission/` dir if needed)
- [ ] Tweet/post-mortem with the project link if user wants public signal (optional; not in PLAN deliverables)

---

## Submission-blocker decision tree

If something below is YES at T-1h, do NOT submit yet — fix or escalate:

- [ ] Live URL returns 5xx? → Redeploy: `gcloud run deploy causal-oncall --source=. --region us-central1` (~3 min build)
- [ ] OAuth client still missing AND demo-mode no longer renders the wow moments? → Stay on demo-mode (it does render all 4 wows); flip OAuth post-submission
- [ ] Repo still private? → Flip via `gh repo edit ... --visibility public`
- [ ] License not detected by GitHub? → Check LICENSE file is at repo root, named exactly `LICENSE` (no `.md`, no `.txt`), and contains the canonical Apache-2.0 text
- [ ] Video runtime > 3:00? → Re-edit, cut beat 5 first (memory wow is narration-only, easiest to trim)

---

## Post-submission (only after Submit click)

- [ ] Revert Cloud Run to `--min-instances=0` to stop the demo-window spend: `gcloud run services update causal-oncall --min-instances=0 --region us-central1`
- [ ] Tag the submitted commit: `git tag -a submitted-2026-06-12 -m "Devpost submission — Dynatrace track" && git push origin submitted-2026-06-12`
- [ ] Capture Devpost submission ID + confirmation screenshot to `causal-oncall/submission/`
- [ ] Add Reddit handle `u/This_Gear1566` to any submission notes if Devpost asks for community/social IDs (per user memory)
