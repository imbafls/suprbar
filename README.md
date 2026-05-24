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

> **Status:** v0.5-dev — usage counter / dashboard. Coach experiment removed;
> focus is multi-source spend tracking and budgets.

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

- **v0.5** — strip coach; tighten usage-bar UX; source tabs in flyout
- **v0.6** — Cursor / Codex CLI local log providers
- **v0.7** — unified multi-source totals + per-source filters
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
