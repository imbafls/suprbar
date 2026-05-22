<div align="center">

<img src="docs/brand/mark-256.png" width="96" alt="supr.bar"/>

# supr.bar

**A coach in your tray, not a counter.**

A tray companion for Windows 11 that watches your local Claude Code sessions
and surfaces specific, actionable observations — when you're iterating in
circles, when you're winning, when to stop.

No login. No telemetry. Just your data, observed.

</div>

---

> **Status:** v0.4-dev. The legacy "usage counter" flyout still works and
> ships in releases; the **coach** rules engine is being built in parallel.
> See [`pivot_v1.md`](./pivot_v1.md) for the plan and
> [`CHANGELOG.md`](./CHANGELOG.md) for history.

## What it does

- Reads `~/.claude/projects/**/*.jsonl` — no API key, no cloud.
- Runs a small set of **rules** against each live session.
- When a rule fires, drops the observation into the tray flyout:
  > **You're iterating in circles.**
  > 73 % of the last 30 minutes was cache reads with three rewrites of
  > `popup.py`. Worth restating the constraint in a fresh prompt.
- When the session goes idle, writes a 3-bullet markdown retro to
  `~/.suprbar/sessions/<date>.md`.

The cost number stays in the corner. It is not the point.

## Install

### From release (recommended)

Download the latest `suprbar-setup.exe` from
[Releases](https://github.com/imbafls/suprbar/releases), run it, done.
suprbar lives in your tray.

### From source

```sh
git clone https://github.com/imbafls/suprbar
cd suprbar
pip install -r requirements.txt
python -m suprbar
```

Requires Python 3.11+, Windows 11 (Win10 should work but is not the primary
target), and a WebView2 runtime (preinstalled on Win11).

## Quick start

1. Launch suprbar. A gradient "S" appears in your system tray.
2. Open Claude Code and start a session.
3. Click the tray icon to see the live flyout.
4. Right-click → Settings to tweak refresh, theme, hotkeys.
5. After your next session ends, find the retro at
   `%USERPROFILE%\.suprbar\sessions\<date>.md`.

## How it works

```
~/.claude/projects/*.jsonl
        │
        ▼
 ┌─────────────────┐
 │  scanner.py     │  incremental, mtime-keyed, parallel pool
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │ coach/context   │  rolling 7-day stats + active session
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │ coach/engine    │  run all Rules, pick the highest-confidence one
 └────────┬────────┘
          ▼
 ┌─────────────────┐
 │  WebView2 popup │  hero observation + small cost chip
 └─────────────────┘
```

Every observation is the output of a small Python class that subclasses
`Rule` and lives in `suprbar/coach/rules/`. Rules are discovered at boot.
See [`docs/extending.md`](./docs/extending.md) to write one.

## Writing a rule (under 30 lines)

```python
# suprbar/coach/rules/my_rule.py
from suprbar.coach.rule import Observation, Rule

class LongMessage(Rule):
    id = "long-message"
    name = "Very long message"
    description = "Nudges when the last user message exceeds 4k chars."

    def evaluate(self, ctx):
        last = ctx.recent_user_messages(limit=1)
        if not last:
            return None
        msg = last[0]
        if len(msg.text) < 4_000:
            return None
        return Observation(
            id=self.id, severity="info", confidence=0.7,
            title="Long prompt",
            body=f"Your last message was {len(msg.text):,} chars. "
                 "Long prompts often hide multiple asks.",
            tip="Split into two turns: state the constraint, then the change.",
        )
```

Drop the file. Restart suprbar. The rule is live. Toggle it in
Settings → Coach.

## Philosophy

- **Observe, don't nag.** Severity tops out at `warn`; supr.bar never
  blocks, modals, or beeps unless you ask.
- **Your data stays local.** Zero phone-home. Anthropic Admin API is opt-in
  only.
- **Plain text wins.** Session retros are markdown files in a folder. No
  database, no proprietary format.
- **Extension over configuration.** The setting panel is small; rules are
  small; if you need more, write a rule.

## Roadmap (high-level)

See [`pivot_v1.md`](./pivot_v1.md) for detail.

- **v0.4 — coach v0** · 6 built-in rules, session wraps
- **v0.5 — extension surface** · rule template, docs, first external rule
- **v0.6 — receipt artifact** · session-card PNG, one-click copy
- **v0.7 — patterns** · weekly Sunday-night summary
- **v1.0 — cross-platform** · macOS + Linux popup ports

## Contributing

PRs welcome on any of:

- New rules (the most useful contribution surface).
- Themes (`suprbar/static/themes/*.css`).
- Data sources beyond local JSONL.
- Translations of observation copy.
- Bug reports with a session JSONL excerpt that reproduces the issue.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

MIT. See [`LICENSE`](./LICENSE).

---

<div align="center">
<sub>Built by <a href="https://github.com/imbafls">@imbafls</a>. Not affiliated with Anthropic.</sub>
</div>
