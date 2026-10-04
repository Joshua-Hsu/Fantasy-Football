# FF Tier Builder — Design System

A small, Robinhood-inspired design system extracted from the Tier Builder web app
(`docs/`). Signature green accent, bold oversized type, soft rounded cards, pill
buttons, generous whitespace, and a true-black dark theme.

## Structure

```
design-system/
  tokens.css                      shared design tokens (colors, radius, shadow)
  overview.html                   single-page showcase of everything
  components/
    foundations/index.html        color, type, spacing, radius
    buttons/index.html            pill buttons — primary / secondary
    cards/index.html              position card + head-to-head card w/ stat tiles
    chips/index.html              tier chip + rookie badge + picks pill
    table/index.html              ranking table
    header/index.html             sticky nav header
```

Each `components/*/index.html` is a standalone preview page whose first line is a
`<!-- @dsCard group="…" -->` marker, so Claude Design's Design System pane indexes
it automatically. Tokens are inlined in each file so the previews render on their
own; `tokens.css` is the canonical source to copy into real projects.

## Tokens

| Token | Light | Dark | Use |
|---|---|---|---|
| `--accent` | `#00c805` | `#00c805` | brand green — fills, active borders |
| `--accent-ink` | `#009e22` | `#27e068` | green text on surface (readable) |
| `--accent-soft` | `#e9faec` | `#0f2417` | tinted chip/press backgrounds |
| `--ink` | `#0b0c0e` | `#f4f6f7` | primary text |
| `--muted` | `#6b7280` | `#98a2ad` | secondary text |
| `--surface` / `--surface-2` | `#fff` / `#f5f6f8` | `#0c0e0f` / `#15181a` | card / inset |
| `--line` | `#e7e9ec` | `#23272b` | borders, dividers |
| `--radius` / `--radius-sm` | `18px` / `12px` | — | card / control radius |

## Principles

- **One green.** `--accent` for fills and active states; `--accent-ink` for text so
  contrast holds on white.
- **Big, tight headlines.** Weight 800, `letter-spacing:-.02em`.
- **Numbers are the hero.** Tabular figures; key stats shown as big-value tiles.
- **Soft, not boxy.** 18px radius + layered shadow instead of hairline borders.
- **Pill everything interactive.** `border-radius:999px` on buttons and chips.

## Syncing to Claude Design

Authorization can't be granted from a cloud session. From an interactive Claude
Code session on your machine, run `/design-login` once, then `/design-sync` and
point it at this `design-system/` directory — it pushes one component at a time.
