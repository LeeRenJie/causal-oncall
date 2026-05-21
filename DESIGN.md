# DESIGN.md — Causal On-Call

## Color strategy

**Restrained.** Tinted neutrals + one accent. The page is mostly black-tinted-toward-deep-blue, with a single signal-green (`oklch(78% 0.18 145)` ≈ `#56d364`) as the only saturated color. Green appears only on: live trace ticks, "confirmed fix" badges, the dashboard's accuracy sparkline, and the primary CTA button. Everything else is shades of near-black.

The scene that forced this: an SRE glancing at the page at 2am on a 27-inch monitor in a dim room, deciding whether to click into the demo. Dark theme is not the SaaS-developer-cliché — it's the only theme that doesn't blast the user's eyes when paged.

| Role | Token | Value | Use |
|---|---|---|---|
| `--bg` | bg | `oklch(14% 0.01 250)` | Page background. Nearly black, tinted toward deep blue. |
| `--surface` | surface | `oklch(18% 0.012 250)` | Card surfaces, raised panels. |
| `--surface-2` | elevated | `oklch(22% 0.014 250)` | Inner panels (e.g. evidence accordion expanded body). |
| `--border` | border | `oklch(28% 0.015 250)` | Hairline borders. 1px only. |
| `--text` | text | `oklch(94% 0.008 250)` | Primary body. |
| `--text-muted` | muted | `oklch(68% 0.012 250)` | Secondary text, labels, footers. |
| `--accent` | accent | `oklch(78% 0.18 145)` | Signal green. Live state, confirmed, top-action. |
| `--accent-soft` | accent-soft | `oklch(78% 0.18 145 / 0.12)` | Accent at 12% alpha — confidence bar fill background, hover states. |
| `--warn` | warn | `oklch(80% 0.15 75)` | Yellow-amber. Medium confidence (0.5–0.8), hypothesis-rejection state. |
| `--danger` | danger | `oklch(70% 0.17 25)` | Red. Low confidence (<0.5), failed cases. |

**Never used:** `#000`, `#fff`, pure brand colors (Slack purple, Mongo green, Google blue) — sponsor pills get a faint border + text in `--text-muted`, not their brand colors.

## Typography

System monospace for technical surfaces, system sans for prose.

```css
--font-mono: ui-monospace, 'JetBrains Mono', 'SF Mono', Menlo, monospace;
--font-sans: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Inter, sans-serif;
```

Scale (1.333 modular):

| Step | Size | Use |
|---|---|---|
| `--text-7xl` | 4.8rem (76.8px) | Hero H1 (one page) |
| `--text-5xl` | 2.7rem (43.2px) | Section heads |
| `--text-3xl` | 1.6rem (25.6px) | Card titles |
| `--text-xl` | 1.2rem (19.2px) | Lead paragraphs |
| `--text-base` | 1rem (16px) | Body |
| `--text-sm` | 0.875rem (14px) | Labels, metadata |
| `--text-xs` | 0.75rem (12px) | Footers, sponsor badges |

Body line length capped at 65ch. Numbers and identifiers (problem IDs, scores, hashes) always in `--font-mono` for tabular alignment.

Weight contrast: 400 body, 600 semibold for emphasis, 700 bold ONLY for the hero H1.

## Layout

- 12-col CSS grid on desktop, 4-col on mobile
- Page max-width 1180px, gutters 32px
- Vertical rhythm: 8px base, multiplied by Fibonacci-ish steps (8, 16, 24, 40, 64, 104)
- Hero gets 104px top + bottom padding; demo card grid gets 24px gap; evidence rows get 8px vertical rhythm

**No outer container around the whole page.** Hero, demos, footer each set their own max-width + margin-auto. The page background is full-bleed.

## Components

| Component | Notes |
|---|---|
| Hero | Left-aligned, monospace overline ("v1.0 · live"), 76.8px sans serif title, 19.2px lead paragraph, single primary CTA (anchor to demo grid). |
| Demo card | 3-up grid. Each card: icon (inline SVG, 32px), title, 2-line description, CTA inside the card. Spring-scale on hover, ripple on tap. |
| Live trace panel | Appears below the demo grid after a click. Monospace lines streaming in, one per agent step. Last line gets a pulsing dot until completion. |
| Hypothesis card | After the brief returns. Card header has rank pill + title + confidence bar. Body is the recommended action. Evidence accordion (collapsed) shows count badge. |
| Confidence bar | Thin (4px height) horizontal bar. Fill color: green ≥0.8, amber 0.5–0.8, red <0.5. Animated fill on mount (350ms ease-out-quart). |
| Sponsor pill | Footer row. Border 1px `--border`, padding 6px 12px, `--text-muted` text, monospace. No brand colors, no brand logos. |
| Confirm/Reject buttons | Primary = filled `--accent`, secondary = outlined `--border`. Spring scale on hover/tap. |

## Motion

Via [motion.dev](https://motion.dev) CDN (vanilla JS, Framer-Motion-style API).

- **Hero mount:** title + lead + CTA stagger in with 0.08s delay between each, 600ms ease-out-quart from 12px below.
- **Demo card grid:** stagger in on `inView()` trigger, 0.08s between cards, spring physics (`{ stiffness: 200, damping: 25 }`).
- **Demo button click:** scale tap (0.97 then back), then trace panel grows in with a height + opacity transition.
- **Trace lines:** new SSE event appends with a 8px slide-up + opacity 0→1, 200ms.
- **Hypothesis cards:** stagger reveal as the brief returns (0.06s delay between cards), spring.
- **Confidence bar fill:** animate from 0% to target% on mount, 350ms ease-out-quart.
- **Numbers (accuracy %, scores):** count-up animation, never instant.
- **Sparkline (dashboard):** stroke-dashoffset draw animation, 1.2s on first paint.
- **Sponsor pills:** subtle 1px translate-up on hover, 150ms.
- **Always respect `prefers-reduced-motion: reduce`** — fall back to instant transitions.

## Icons

Inline SVG, 24px viewBox, `currentColor` strokes. No external icon libraries.

| Need | Approach |
|---|---|
| Demo button icons | Hand-rolled SVGs: cold = snowflake-meets-clock; memory-hit = bookmark with check; rejection = branching arrow |
| Status icons | Pulsing dot (live), check-circle (confirmed), x-circle (rejected) |
| Specialist tags | Single letter inside a rounded square: T (triage), Y (topology), D (deploy), A (anomaly), V (vuln) — minimal, no avatar nonsense |
| Sponsor pills | TEXT ONLY. No brand logos (legal + visual noise). |

## What we don't do (anti-patterns from impeccable's bans + our own)

- ❌ No gradient text
- ❌ No glassmorphism / backdrop-blur as decoration
- ❌ No side-stripe borders on cards
- ❌ No hero-metric template (big-number + small-label + supporting-stats + gradient accent)
- ❌ No identical card grids of icon+heading+text
- ❌ No modals — everything inline or progressive disclosure
- ❌ No em dashes in copy (use commas, colons, periods, parens)
- ❌ No "Built with ❤️" or AI sparkles
- ❌ No carousels, no auto-playing video, no scroll-jacking
- ❌ No drop shadows on cards — flat surfaces with hairline borders only
