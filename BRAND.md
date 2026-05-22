# supr.bar — Brand identity

A minimal kit so the project looks consistent across the README, GitHub Pages,
and any social card / blog post. Designed to be writeable by hand — no Figma
file required.

---

## Name

**supr.bar** — lowercase, period. Used as one word. Domain-style on purpose.

- Display: `supr.bar`
- Repo-safe: `suprbar` (no dot, lowercase)
- Sentence usage: "supr.bar watches your sessions and surfaces notes."

Never `Suprbar`, `Supr.Bar`, `SUPR.BAR`, or `SuperBar`.

---

## Tagline

**A coach in your tray, not a counter.**

Short alt (≤ 30 chars for social cards):

- "Watch your sessions."
- "Notice your patterns."
- "Not just a counter."

One-paragraph descriptor (use on the GitHub repo "About" and the site hero):

> supr.bar is a tray companion that watches your local Claude Code sessions
> and surfaces specific observations — when you're iterating in circles,
> when you're winning, when to stop. No login. No telemetry. Just your data,
> observed.

---

## Mark

A diagonal gradient tile with a centered "S" glyph.

| Token | Value |
|---|---|
| Shape | Rounded square, corner radius **19 %** of side |
| Gradient direction | 135 deg (top-left → bottom-right) |
| Gradient stops | `#5b8def` 0 % → `#8c5bef` 100 % |
| Glyph | Single uppercase **S**, Segoe UI Black / Inter Bold |
| Glyph fill | `#ffffff` |
| Glyph optical centering | Shift down by ~3.5 % of side height |

Sizes shipped in repo:

- 16, 24, 32 (favicon set)
- 64 (Windows tray runtime)
- 256 (PyInstaller .exe icon, app stores)
- 1024 (social cards, README hero)

When the active session has a coach observation of severity `warn`, the
mark recolors to `--b-warn → #f59e0b`. For `nudge` it stays default. The
`Severity → Palette` map is documented in `BRAND.md` so contributors can
ship matching theme variants.

---

## Color tokens

These match `:root` in `suprbar/static/styles.css`. Treat them as the
canonical brand palette.

```css
--b-accent:        #5b8def;   /* primary action, links, focus rings */
--b-accent-hot:    #7aa8ff;   /* hover / active state of accent */
--b-violet:        #8c5bef;   /* mark gradient end, secondary highlight */
--b-success:       #4ade80;   /* "you're winning" observations */
--b-warn:          #fbbf24;   /* "nudge" observations */
--b-danger:        #f87171;   /* "warn" observations, over-budget */
--b-claude:        #d97757;   /* claude provider chip */

/* surfaces */
--bg-grad-top:     #1a1f2e;
--bg-grad-bot:     #0d1018;
--surface:         rgba(28, 30, 38, 0.92);
--surface-2:       rgba(255,255,255,0.025);
--hairline:        rgba(255,255,255,0.06);

/* text */
--text:            #f4f5f7;
--text-dim:        rgba(255,255,255,0.55);
--text-dimmer:     rgba(255,255,255,0.45);
```

Light theme is opt-in (`[data-theme="light"]`); the brand stays dark by
default because the app lives in a tray flyout against the Win11 dark
shell.

---

## Typography

| Use | Family | Weight | Notes |
|---|---|---|---|
| Numerals, tags, code | **Geist Mono** | 400 / 500 | Always tabular-nums |
| Headings, body | **Geist Sans** | 400 / 500 / 600 | Letter-spacing -0.01em on 20px+ |
| Fallback (offline / system) | Segoe UI, ui-sans-serif, system-ui | — | First-class Windows fallback |

Don't introduce a third typeface. If you must, replace Geist Mono with JetBrains
Mono or IBM Plex Mono — but pick one, ship it everywhere.

---

## Voice

- Second-person, observational, constructive.
- Sentence case. No emoji. No exclamation marks.
- Tip lines start with a verb: "Break and clarify." / "Switch to sonnet."
- Avoid imperatives unless the rule severity is `warn`.
- Never moralise about cost. The user has a subscription; respect that.
- Never compare to other users. We don't have other users' data and never will.

Example observation copy:

> **You're iterating in circles.**
>
> 73 % of the last 30 minutes was cache reads with three rewrites of
> `popup.py`. Worth a fresh prompt that restates the constraint.
>
> *Tip:* close the current message, write one sentence describing what
> "done" looks like, paste it as a new turn.

---

## Logo download set

All sourced from `suprbar/static/brand/`:

```
suprbar/static/brand/
  mark-16.png
  mark-24.png
  mark-32.png
  mark-64.png
  mark-256.png
  mark-1024.png
  mark.svg            # vector source
  wordmark-light.svg  # "supr.bar" lockup, light text
  wordmark-dark.svg   # "supr.bar" lockup, dark text
  social-card.png     # 1200×630 GitHub/X/OG image
```

Generation: see `scripts/build-brand-assets.py` (Pillow, no Figma needed).

---

## Don'ts

- Don't stretch the mark — always uniform.
- Don't replace the "S" with another glyph.
- Don't put the mark on a non-dark background without the wordmark wordmark
  underneath; the gradient loses contrast on white.
- Don't use the brand colours for arbitrary UI accents; reserve them for the
  meaning attached (success, warn, danger, accent).
- Don't promise outcomes ("ship 2× faster"). The voice is observation, not
  marketing.
