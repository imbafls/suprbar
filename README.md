<div align="center">

<img src="docs/brand/mark-256.png" width="96" alt="supr.bar"/>

# supr.bar

**What your coding agents cost, in your tray.**

A small Windows 11 tray app that reads your local Claude Code sessions (plus
opencode, Hermes and optional API accounts) and shows today's spend, your live
burn rate and where the money went — local-first.

No login. No telemetry. Your data stays on your machine.

</div>

---

> **Status:** v2.0 — trimmed and stable. Five settings, a six-item tray menu,
> ~40 MB and one process while the flyout is closed.

## What it does

- **Today at a glance** — cost, messages, tokens, and while a session is live,
  its burn rate ($/h) and project.
- **Ranges** — Today · 7d · 30d · 90d, with hourly or daily bars.
- **Where it went** — top projects and top models for the range.
- **Sources** — Claude Code (`~/.claude/projects/**/*.jsonl`, no key needed),
  opencode (its SQLite db), Hermes (`~/.hermes/sessions/sessions.json`), and
  optional API accounts: Anthropic Admin, OpenRouter, OpenAI. Each source gets
  a chip with its cost; a source that fails shows `!` and its error instead of
  a silent $0.00.
- **Mini overlay** — an always-on-top chip with the last 24h, live dot and
  $/h. Hover for details, click for the flyout, × to turn it off.
- **30-day report** — a full-page report in your browser (tray menu or the
  flyout's Report button).

## Install

### From release (recommended)

Download `suprbar-setup-<version>.exe` from
[Releases](https://github.com/imbafls/suprbar/releases) and run it.

### From source

```sh
git clone https://github.com/imbafls/suprbar
cd suprbar
pip install -r requirements.txt
python -m suprbar
```

Requires Python 3.11+, Windows 11 and WebView2 (preinstalled on Windows 11).

## Using it

- **Click the tray icon** to open or close the flyout. It opens at the
  bottom-right of the screen you're on and hides when you click elsewhere —
  unless you pin it (the pin button, or **middle-click** the tray icon).
- **Right-click the tray icon** for: Open supr.bar · Mini overlay · Refresh ·
  30-day report · Settings… · Quit.
- **Settings** (gear in the flyout) — that's all of them:

  | Setting | What it does |
  |---|---|
  | Sources | Turn each source on or off; paste, test or clear API keys |
  | Mini overlay | Show the always-on-top 24h chip |
  | Keep flyout open | Don't hide the flyout when you click elsewhere |
  | Start on login | Launch supr.bar when you sign in |
  | Check for updates | Look for a new release at launch and every 6 hours |

  Every change applies immediately.

## Updating

supr.bar updates itself. When a release is out you get an **Update to vX**
button in the flyout and an item in the tray menu. The installer is downloaded
from the GitHub release, checked (HTTPS + host allowlist, installer name, the
release's SHA-256, a size ceiling), run silently, and supr.bar restarts into
the new version; any failure leaves your install untouched. Turn off **Check
for updates** and supr.bar never checks on its own. Source checkouts never
auto-update — use `git pull`.

The installer isn't code-signed yet, so SmartScreen may warn: **More info →
Run anyway**.

## Privacy

Everything is read locally. supr.bar serves its pages from `127.0.0.1` only.
It makes two unauthenticated requests of its own: the release check above
(unless you turn it off) and the hosted pricing table (daily, so new models
price correctly without a release). API sources you turn on talk to their
provider with the key you gave them. Keys are stored DPAPI-encrypted.

## How it stays light

- The tray process never loads WebView2. The flyout runs in its own process,
  started when you open it and ended a minute after you close it; the mini
  overlay's process exists only while it's on.
- Nothing polls while the flyout is hidden.
- The today scan only opens session files written in the last 25 hours, and
  range tabs read a per-day index (`%LOCALAPPDATA%\suprbar\scan-index.json`),
  so a large history is parsed once, not on every click.

## Architecture

```
~/.claude/projects/*.jsonl   opencode.db   ~/.hermes/…   API accounts
          │                      │              │              │
          ▼                      ▼              ▼              ▼
      scanner.py ──► providers/local.py, opencode.py, hermes_local.py, …
                                   │
                                   ▼
                            aggregator.py ──► server.py (127.0.0.1)
                                                   │
                  tray.py ── windows.py ──► flyout / mini child processes
                                             (windowhost.py → WebView2)
```

A new data source is a provider in `suprbar/providers/`, registered in
`aggregator.py`. See [`docs/extending.md`](./docs/extending.md).

## Contributing

PRs welcome — new data sources, bug fixes, docs. See
[`CONTRIBUTING.md`](./CONTRIBUTING.md).

## License

MIT. See [`LICENSE`](./LICENSE).

---

<div align="center">
<sub>Built by <a href="https://github.com/imbafls">@imbafls</a>. Not affiliated with Anthropic.</sub>
</div>
