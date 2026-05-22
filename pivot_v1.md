# supr.bar — pivot_v1

**Status:** draft · 2026-05-22
**Author:** Omer Taji (@omer)
**Repo state:** working CodexBar-style tray + 74-setting panel + 100-improvement
agent fan-out. This document specifies the pivot.

---

## TL;DR

We are pivoting from a **usage counter** ("CodexBar for Windows") to a
**coach**. The tray flyout stops being a passive dashboard and becomes an
observer that surfaces specific, actionable notes about your current Claude
Code session and your patterns over time.

Cost numbers stay, but secondary. The hero of the UI is the **next thing you
should know about your session**.

---

## Why we are pivoting

Honest read of v0.3:

1. **"Equivalent cost on a Max subscription" is theatre.** People stop caring
   the moment they realise it isn't real money. There's nothing to act on.
2. **The flyout is read-only.** Glance, close. No decision it helps you make.
3. **74 settings is the symptom of a thin product.** Every choice becomes a
   toggle when the core proposition is unclear.
4. **The category is commoditized.** ccusage, claude-monitor, codexbar,
   claude-code-statusline, n more — counters are not a moat. Anthropic
   shipping `/usage` natively is when this kind of app gets erased.
5. **The data we already collect can answer better questions** than "how
   much did I spend?" — namely: am I making progress, am I stuck, when should
   I stop, what patterns am I repeating.

The technical bones from v0.0 — v0.3 are solid (scanner, popup chrome,
settings system, range cache). The pivot is about **what we show**, not
**how we built it**.

---

## The new product, in one sentence

**supr.bar is a tray companion that watches your local Claude Code sessions
in real time and surfaces specific, actionable observations — when you're
winning, when you're iterating in circles, when to stop.**

Three bullets:

- **No login. No telemetry. No cloud.** The entire app reads
  `~/.claude/projects/**/*.jsonl` locally. The optional Anthropic Admin API
  source stays available, off by default.
- **Observations, not dashboards.** The flyout's primary surface is the
  single most useful note for *this session, right now*. The number is still
  there, smaller.
- **Session "wraps."** When a session goes idle for ≥ 10 minutes, supr.bar
  writes a 3-bullet markdown retro to `~/.suprbar/sessions/`. That file is
  the shareable artifact — the dashboard is not.

---

## Architecture: the Rules Engine

Everything new lives behind one interface.

```python
# suprbar/coach/rule.py
from dataclasses import dataclass

@dataclass(frozen=True)
class Observation:
    id: str              # stable rule id, e.g. "circling-in-cache"
    severity: str        # "info" | "nudge" | "warn"
    title: str           # one short line, sentence case, no period
    body: str            # one to three sentences
    tip: str | None      # optional concrete next action
    confidence: float    # 0.0 - 1.0; UI suppresses below 0.5

class Rule:
    id: str
    name: str            # human label
    description: str     # one-liner for the rule directory

    def evaluate(self, ctx: "SessionContext") -> Observation | None:
        """Inspect the session + recent history. Return None if no signal."""
        raise NotImplementedError
```

`SessionContext` is a read-only view over:

- The currently-active session's parsed JSONL (last N messages, full per-msg
  usage fields, file references from tool_use blocks).
- The user's rolling stats (7-day median cost/hour, model mix, project
  history, time-of-day cost curve).
- Recent git activity for the current project's working dir (commit count,
  files changed since session start).

The coach runs the active rule set every poll cycle (default 5 s while the
popup is visible, 60 s while hidden). The highest-confidence observation
wins the hero slot. Lower-confidence observations are stacked in a small
"more notes" drawer.

### Rule discovery

Rules live in `suprbar/coach/rules/*.py`. The coach imports every Python
file in that directory at boot and registers any class that subclasses
`Rule`. Adding a rule = drop a file. No registration list, no plugin
manifest, no schema.

User opt-out per rule: a Settings → Coach panel with one toggle per rule
(default all on) so privacy-conscious users can mute the patterns they don't
want surfaced.

---

## MVP scope — v0.4 "coach v0"

Six built-in rules. Pick the smallest cut that proves the pattern is useful.

| Rule id | Title | Fires when |
|---|---|---|
| `circling-in-cache` | You're iterating in circles | Last 30 min: cache_read ÷ (input + cache_read) > 0.7 AND tool_use rewrites of same file ≥ 3 |
| `hot-file` | This file has been edited a lot | Same file path appears in ≥ 10 tool_use blocks within the session |
| `burn-spike` | Today's burn is well above your norm | Rolling 24 h cost ÷ 7-day median ≥ 3× |
| `late-night-tax` | After-hours sessions ship less | Session started after 22:00 local AND user's history shows ≥ 2× rewrite rate for late sessions |
| `wrong-model-cheap-job` | Sonnet would have done this | Opus, ≥ 4 messages in, thinking blocks = 0, no file edits yet |
| `time-to-stop` | Hour 4. Diminishing returns kicked in at hour 2.7 | Commits/messages ratio dropping vs. session start AND ≥ 3 h elapsed |

For each fire, supr.bar:

1. Renders the observation in the hero slot.
2. Plays an optional one-shot sound (off by default).
3. Writes the observation to `~/.suprbar/observations.jsonl` so the wrap-up
   has data to summarise.

Idle-detection (10 min of no new JSONL writes) triggers `wrap.py` which
calls every rule's `wrap_up(ctx)` method (optional) and renders the day's
markdown retro.

---

## What we keep from v0.0 – v0.3

- `suprbar/scanner.py` — JSONL walker, incremental cache, parallel pool.
- `suprbar/pricing.py` — per-model rates with 1M premium.
- `suprbar/popup.py` — frameless tray popup, DWM corner round, multi-monitor.
- `suprbar/tray.py` — gradient icon, palettes (we'll reuse for severity).
- `suprbar/server.py` — local HTTP server (we lose a lot of routes — see below).
- `suprbar/config.py` — DPAPI key storage, schema/migration, window state.
- The settings overlay shell + dynamic control rendering.

## What we throw out

- **All the range tabs (24h / 7d / Wk / Mo / 30d / 90d)** — coach is about
  *this session* and rolling patterns, not 90-day windows.
- **Budgets UI** (`budgets.*`, `/api/budgets`, the budget strip) — irrelevant
  on a subscription.
- **Project allowlist/denylist + anonymize** — coach watches what you're
  actually doing; the data is yours regardless.
- **`display.show_*` toggles** (15 of them) — the coach decides what to
  show, not 15 booleans.
- **`projects.top_n`, hourly histogram, by-model table** — backend stays,
  the UI surfaces are gone.

The settings panel collapses from 74 entries to roughly **12**: theme,
accent, density, font-scale, autostart, pin, refresh interval, anonymize
project paths in *exported* markdown, log level, hotkeys for show/quit, and
the per-rule mute list.

---

## Roadmap

| Version | Theme | Done when… |
|---|---|---|
| **v0.4 — coach v0** | Six rules + session wrap | Hero slot shows live observations; idle writes `~/.suprbar/sessions/<date>.md` |
| **v0.5 — extension surface** | Plugin-author docs, rule template, one community rule merged | `docs/extending.md` exists; CI runs rule tests; one external PR landed |
| **v0.6 — receipt artifact** | Session-end shareable PNG + clipboard | One-click "copy session card" from tray menu |
| **v0.7 — patterns** | Weekly patterns view | Sunday 9pm summary covers: top-3 winning sessions, top-3 stuck sessions, suggested experiments for next week |
| **v0.8 — optional integrations** | First-party Anthropic Admin API source rebuilt around coach signals; first-party git attribution | "Today you committed X lines across Y files in Z sessions" |
| **v1.0** | Cross-platform | Mac + Linux ports of the popup. Coach + scanner are already platform-agnostic. |

---

## Extension points (this is how the project survives)

The product is **the coach**. The platform is **how others write coaches**.

1. **Rules** — drop a `.py` file into `suprbar/coach/rules/` (project) or
   `~/.suprbar/rules/` (user). Each rule has a 50-line skeleton with type
   hints; we publish a `cookiecutter`-style `make rule` helper.
2. **Data sources** — beyond local JSONL and Anthropic Admin API, a contributor
   can implement `Source` (existing interface) for codex CLI, Cursor's local
   log, Gemini CLI, etc. The coach is agnostic to where data comes from.
3. **Wraps** — the markdown writer is templated. Drop a template into
   `~/.suprbar/templates/` to change the retro format.
4. **Themes** — CSS theme variables already exist (`[data-theme="…"]`). Ship
   a theme by dropping a CSS file in `suprbar/static/themes/`.
5. **Tray menu actions** — small API for contributors to add tray menu
   items that call HTTP endpoints.

Every extension type gets one page in `/docs` with a complete working
example.

---

## Non-goals (write these down so we don't drift)

- **No social network.** No public profiles, no leaderboards, no "share to
  feed". Receipts are local files; users post them themselves where they
  want to.
- **No required cloud.** Optional integrations only. The app is fully
  functional offline.
- **No "AI suggestions" beyond rule-based observations.** v0 rules are
  deterministic. ML may come later for pattern detection, but never for
  generating advice copy.
- **No prescriptive coaching.** The coach observes; it does not nag. Tone is
  second-person constructive, not imperative.
- **No paid tier.** MIT-licensed, free, no upsell. Sponsorship via GitHub
  Sponsors is the only monetisation; non-blocking.
- **No telemetry.** Zero phone-home. We don't even count installs.

---

## Open questions

1. **Idle detection threshold.** 10 min is a guess. Need to instrument and
   tune. Should it be model-aware? (Opus sessions naturally have longer
   gaps.)
2. **Rule severity calibration.** When two rules fire, who wins the hero
   slot? Confidence-weighted? Last-fired? User-favourited?
3. **Privacy of the wrap.** Default-anonymized project paths in the
   exported markdown? Default-clear and let users redact? I lean toward
   anonymize-by-default with a "show real paths" toggle.
4. **Cross-machine.** A laptop + desktop user has two `.claude` dirs. Do
   we merge? v0.4 says no — single machine. Later: optional sync via a
   simple shared SQLite file in OneDrive/iCloud.

---

## First commit on this branch

```
git checkout -b pivot/coach-v0
# scaffold:
mkdir -p suprbar/coach/rules
touch    suprbar/coach/__init__.py
touch    suprbar/coach/rule.py        # Observation + Rule base
touch    suprbar/coach/context.py     # SessionContext builder
touch    suprbar/coach/engine.py      # runs all rules, picks hero
mkdir -p suprbar/coach/rules
# delete:
git rm -r suprbar/static/styles/range-tabs.css   # (and the range-tab DOM block)
# disable but don't delete (yet):
#   /api/range, /api/budgets, budget UI, project allow/deny UI
```

After scaffold: ship `circling-in-cache` end-to-end before touching anything
else. If the observation doesn't *feel* useful inside the popup, the whole
pivot is wrong and we want to know before refactoring the UI shell.
