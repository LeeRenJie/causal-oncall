# PRODUCT.md — Causal On-Call

## Register

**Brand.** This is the judges' landing page for a hackathon submission. The page IS the product impression. The pitch and the polish are the product.

## What it is

Causal On-Call is an ADK multi-agent SRE assistant. When a Dynatrace problem opens, six agents (orchestrator + 5 specialists + synthesizer) run the exact pre-mortem an experienced SRE would do — DQL composition, deploy correlation, topology blast-radius, anomaly windows, vulnerability cross-check — and produce a ranked causal hypothesis brief with one recommended action. Posted to Slack + written back to Dynatrace Grail as a custom event, within ~90 seconds.

On day 1 it saves 14 minutes per incident. By month 6 it short-circuits 35% of incidents from memory (Mongo Atlas vector match). By year 1, the agent's hypothesis-accuracy curve rises from 41% to 73%.

## Users — primary audience for this surface

**Hackathon judges**, specifically the Dynatrace bucket:
- Sean O'Dell — Principal PMM, Developer Experience at Dynatrace
- Jeff Blankenburg — Principal Developer Advocate at Dynatrace
- Plus 7 Google Cloud Partner Engineers reviewing across all 6 partner buckets

What they value (inferred from their roles):
- The partner's product (Dynatrace MCP) is **load-bearing**, not glued on
- Demos that solve a problem a Dynatrace customer would recognize
- Polish that signals senior engineering, not weekend hackathon vibes
- Visible reasoning — they want to see the agent's plan, not just its output

They watch a 3-minute video. They also click the live URL — usually in incognito, on a laptop, alongside the demo video.

## Brand tone

- **Engineering-confident, not salesy.** Like a senior SRE wrote it. No marketing fluff.
- **Specific, not generic.** "Roll back deploy v411 and restore the connection pool to 100" — not "AI-powered incident management."
- **Calm under pressure.** Dark theme. Low chroma. Heavy whitespace. The opposite of the panic that an incident actually causes.
- **Concrete numbers.** $0.28/incident. 14 minutes saved. 92% similarity. 268 tests. 100% coverage.

## Anti-references — what we are NOT

- Not Datadog's marketing site (corporate gradient washes, hero-metric template)
- Not New Relic's purple-everywhere brand reflex
- Not "ChatGPT for SRE" generic AI wrapper energy
- Not Heroku's pastel-illustration whimsy
- Not GitHub Copilot's "the assistant is your friend" anthropomorphism
- Not a Vercel deploy preview (too cream, too restrained, no point of view)
- Not Notion's heavy-card grid
- Not any landing page that uses "powered by AI ✨" anywhere

We are closer to: Linear's product page (precise, technical, fast). Vercel's typography brand voice. Stripe Sigma's docs (data forward, narrow column, opinionated). Anthropic's research pages (calm, monospaced accents, exact numbers).

## Strategic principles

1. **Demo > pitch.** The 3 demo buttons are the hero. The pitch text supports them, doesn't precede them at body length.
2. **Show the agent's plan, not just its output.** The live SSE trace is what differentiates "agent" from "chatbot."
3. **Partner credit, not partner clutter.** Dynatrace is the bucket claim — name it once, name it well. The other sponsors get a clean footer row.
4. **Numbers carry the proof.** Every claim ($0.28/incident, 14 min saved, 92% match) is anchored to a real artifact in the repo or COST-LOG.
5. **No motion theater.** Motion serves comprehension (stagger reveals hierarchy; spring physics make tap responses feel real). No looping animations, no auto-playing carousels, no scroll-jacking.

## Constraints

- Vanilla HTML+JS+CSS architecture — no React, no build step (locked in W3-S5 + W4-S5 precedent)
- Motion via [motion.dev](https://motion.dev) CDN (vanilla-JS Framer-Motion-style API, same authors)
- Icons inline-SVG only, no image assets
- Cloud Run serves it; cold-start latency is a real constraint
- 100% line + branch test coverage gate must hold (HTML files exempt; route handlers covered)
