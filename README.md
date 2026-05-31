<div align="center">

<img src="docs/brand/mark-256.png" width="96" alt="supr.bar"/>

# supr.bar

**API usage in your tray.**

A Windows 11 tray companion that reads your local Claude Code sessions (and
optional Anthropic Admin API data) and shows spend, limits, and burn rate in a
small flyout — CodexBar-style, local-first.

No login. No telemetry. Your data stays on your machine.

</div>

---

> **Status:** v0.10 — in-app auto-update. supr.bar checks GitHub for new
> releases and installs them in one click (opt-out, no telemetry, unsigned —
> SmartScreen may warn). Builds on the glance-first flyout, the 30-day usage
> report, and the lean, fully-wired settings surface.

## What it does

- Reads `~/.claude/projects/**/*.jsonl` — no API key required for local mode.
- Optional **Anthropic Admin API** for org-wide actual spend (Settings → Sources).
- **Range filters** — today, 24h, 7d, week, month, 30d, 90d.
- **Budgets** — daily / weekly / monthly limits with tray warnings.
- **Per-source breakdown** — local Claude Code vs Admin API (more sources planned).
- Live session indicator, burn rate, cache stats, top projects.

## Install

### From release (recommended)

Download the latest `suprbar-setup.exe` from
[Releases](https://github.com/imbafls/suprbar/releases), run it, done.

### From source

```sh
git clone https://github.com/imbafls/suprbar
cd suprbar
pip install -r requirements.txt
python -m suprbar
```

Requires Python 3.11+, Windows 11, and WebView2 (preinstalled on Win11).

## Quick start

1. Launch suprbar — gradient **S** in the system tray.
2. Open Claude Code and start a session.
3. Click the tray icon for the flyout (cost, tokens, burn, budgets).
4. Right-click → **Settings** for refresh, theme, sources, budgets.
5. Set daily/weekly/monthly limits under **Budgets** if you want warnings.

## Updating

supr.bar updates itself — there's no separate updater to run.

- **Automatic check.** Once per launch, in the background, supr.bar asks GitHub
  for the latest release. If a newer one exists you get an **Update available**
  banner in the flyout, a tray notification, and an **Update to vX…** item in the
  tray menu. You can also check on demand: **Updates** in the flyout footer,
  **Check now** under Settings → About, or **Check for updates** in the tray menu.
- **One-click install.** Click **Update** and supr.bar downloads the installer
  asset from the GitHub release, verifies it (HTTPS + host allowlist,
  installer-name allowlist, SHA-256 against the release digest, size ceiling),
  runs it silently, and restarts into the new version. Any failure aborts cleanly
  and leaves your current install untouched.
- **Opt out.** Don't want the launch-time check? Turn off **Settings → Updates →
  Check on launch** (`updates.check_on_launch`). supr.bar then never reaches out
  on its own — manual checks still work.
- **Local-first, no telemetry.** The only network call is an unauthenticated
  `GET` for the latest-release version — no account, no token, nothing about you
  is sent. supr.bar has no server and binds no port; everything stays on your
  machine.
- **Not code-signed (yet).** This build isn't code-signed, so Windows
  **SmartScreen may warn** when the installer runs. That's expected — choose
  **More info → Run anyway**. Running from a source checkout never auto-updates;
  update it with `git pull` instead.

## Architecture

```
~/.claude/projects/*.jsonl          Anthropic Admin API (optional)
        │                                      │
        ▼                                      ▼
 ┌─────────────┐                      ┌──────────────────┐
 │ providers/  │                      │ providers/       │
 │ local.py    │                      │ anthropic_api.py │
 └──────┬──────┘                      └────────┬─────────┘
        │                                      │
        └──────────────┬───────────────────────┘
                       ▼
              ┌─────────────────┐
              │  aggregator.py  │  merge sources → /api/today
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │  WebView2 popup │  cost hero + range tabs + budgets
              └─────────────────┘
```

Adding a new AI/tool = implement a provider in `suprbar/providers/` and
register it in `aggregator.py`. See [`docs/extending.md`](./docs/extending.md).

## Roadmap (high-level)

- **v0.10** _(this release)_ — in-app auto-update (background + manual check, one-click install from GitHub, opt-out)
- **next** — Cursor / Codex CLI local log providers; unified multi-source totals + per-source filters; code-signed installer (drop the SmartScreen warning)
- **v1.0** — macOS + Linux tray ports

## Contributing

PRs welcome: new **data sources**, themes, bug fixes, docs.
See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

MIT. See [`LICENSE`](./LICENSE).

---

<div align="center">
<sub>Built by <a href="https://github.com/imbafls">@imbafls</a>. Not affiliated with Anthropic.</sub>
</div>
