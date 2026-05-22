# supr.bar

Windows tray app for Claude Code (+ Anthropic API) token usage and equivalent
cost. Frameless flyout that pops out of the system tray, built to the supr.bar
design spec at `design/supr-bar/project/`.

## Run

```bash
pip install -r requirements.txt
python -m suprbar
```

Or double-click `run.bat` for a silent launch (no console window).

## Features (MVP)

- **Tray icon + popout** — left-click to toggle the flyout, right-click for menu
- **Active session view** — "Today · Claude Code" cost, token-mix bar, Messages /
  Model / Started metric trio, live pulse indicator
- **Idle view** — empty state with "last seen N ago"
- **Local source** — reads `~/.claude/projects/**/*.jsonl`, no API key needed
- **Anthropic API source** — adds Admin API usage + cost (org-wide) via
  `sk-ant-admin01-…` key. Stored DPAPI-encrypted in `%APPDATA%\suprbar\config.json`
- **Settings overlay** — gear icon → toggle sources, paste & test API key,
  toggle pin / start-on-login
- **Pin** — header pin button or Settings toggle; disables auto-hide on blur
- **Start on Windows sign-in** — writes HKCU\\…\\Run entry to `run.bat`
- **Keyboard** — Esc closes, F5 refreshes, Alt+Q quits, Ctrl+, opens settings
- **Drag** — click & drag the header to reposition

## Architecture

```
suprbar/
  __main__.py          python -m suprbar
  popup.py             pywebview frameless popout + Win32 DWM corner round
  tray.py              pystray gradient icon + menu + tooltip
  server.py            127.0.0.1 HTTP server / static / api routes
  scanner.py           ~/.claude/projects/**/*.jsonl walker + today summary
  aggregator.py        combine all sources into a single response
  config.py            %APPDATA%\\suprbar\\config.json + DPAPI for secrets
  pricing.py           per-token rates (edit when Anthropic adjusts)
  providers/
    local.py           wraps scanner
    anthropic_api.py   /v1/organizations/{cost,usage}_report
  static/              index.html, app.js, styles.css (Geist + Geist Mono)
run.bat                silent launcher
```

## Source: local (default, no key required)

Walks every JSONL session log, sums `message.usage` for today (local day),
detects an active session by JSONL `mtime` within the last 60s.

Costs are computed at current Anthropic per-token rates (see `pricing.py`).
You actually pay $0 on a Max subscription — this is the *equivalent API spend*.

## Source: Anthropic API (optional)

Requires an admin-scoped key from your org settings. The key is stored
DPAPI-encrypted (Windows user-scoped). The Test button hits a 1-day cost-report
range to verify before saving.

Endpoints used:
- `GET /v1/organizations/cost_report?bucket_width=1d` — today's spend (USD)
- `GET /v1/organizations/usage_report/messages?bucket_width=1h&group_by[]=model`
  — token counts for today

## Config file

Plain JSON at `%APPDATA%\suprbar\config.json`. The admin key is a DPAPI blob
inside `sources.anthropic_api.admin_key_enc`. Safe to delete to reset all
preferences.
