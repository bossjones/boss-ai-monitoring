# Design tokens — "signal on slate"

The palette/typography contract `web/static/style.css` implements, and what
[`AGENT_LOOP.md`](AGENT_LOOP.md) critiques future edits against. A light engineering surface with
one job: make cost, health, and failure instantly distinguishable at a glance, in either color
scheme.

## Color

| Token | Light | Dark | Meaning |
|---|---|---|---|
| `--bam-ink` / `--bam-text` | `#1c2733` | `#e6ecf1` | primary text — deep slate |
| `--bam-surface` | `#f4f6f8` | `#141b22` | page background |
| `--bam-surface-raised` | `#ffffff` | `#1c2733` | cards, nav, tiles |
| `--bam-border` | `#d5dce1` | `#2c3a47` | hairlines, dividers |
| `--bam-text-muted` | `#5b6b78` | `#93a3b0` | labels, footers, secondary text |
| `--bam-teal` | `#0e7c7b` | same | **live / healthy** — active sessions, success, sparkline fill |
| `--bam-amber` | `#b57614` | same | **cost** — every dollar figure |
| `--bam-coral` | `#c0504d` | same | **failures** — errors, drift-stale, tool failures |

Both schemes are always styled (`@media (prefers-color-scheme: dark)` as the default signal); the
three semantic accents (teal/amber/coral) stay identical across light and dark so their meaning
never shifts with the viewer's OS setting.

**Rule:** color is never decorative. If a value isn't a cost, a live/healthy signal, or a failure,
it uses `--bam-text` or `--bam-text-muted`, not an accent.

## Typography

- **Data** (numbers, IDs, timestamps, code, table cells) — `--bam-font-mono`: `"SF Mono",
  "JetBrains Mono", ui-monospace, monospace`. Anything the user might compare or copy.
- **Prose** (headings, labels, nav, empty states) — `--bam-font-sans`: the system sans stack
  (`-apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif`). No webfonts — nothing to vendor,
  nothing to wait on.

## Spacing & shape

- `--bam-space` (`1rem`) is the one spacing unit; multiples of it, not ad-hoc pixel values.
- `--bam-radius` (`6px`) on every card/tile/button — soft enough to read as "engineering tool,"
  not sharp enough to read as "spreadsheet."

## Applying the tokens

- Every stat tile: label in `--bam-text-muted` + sans, value in the matching font (mono for
  numbers) and, if it's a cost/live/failure figure, the matching accent color.
- The provenance footer (every panel) uses `--bam-text-muted` for source/freshness text and the
  three accents for `drift-status` (`ok` = teal, `stale` = coral, `unknown` = muted, no accent).
- Charts (the cost sparkline) fill with `--bam-teal`; a bar must stay visually present even at a
  literal `$0.00` value — see AGENT_LOOP.md's worked example, where an invisible zero-cost bar was
  the very first thing the loop caught.
