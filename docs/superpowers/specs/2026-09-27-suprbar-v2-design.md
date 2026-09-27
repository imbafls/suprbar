# supr.bar v2.0 — trimmed, stable, light

Date: 2026-09-27 · Status: approved direction (approach B), spec for implementation

## Why

v0.16 fixed memory (one ~30–55 MB tray process; WebView2 only while a window
exists), but the product is still hard to live with: 41 settings, an
11-item tray menu, a 2,660-line flyout script, 25 HTTP endpoints. Omer
reports all four bug classes: the flyout misbehaves (blank, won't
open/close, hides unexpectedly, wrong position/size), numbers are wrong or
stale, the app gets stuck / leaves zombies / starts slowly, and settings are
confusing. Most of that lives in accreted flyout logic and in options nobody
uses. v2 removes that code instead of patching it.

## What Omer keeps (asked 2026-09-27)

- Today's cost, live dot and burn $/h
- Range tabs, per-project and per-model breakdowns
- Sources: Claude Code (local JSONL), opencode, Hermes, and the API-key
  sources (Anthropic Admin, OpenRouter, OpenAI) with key entry + Test
- The mini overlay (rolling 24h chip)
- The 30-day report page
- Single-instance and auto-update

Everything else goes (list in "Removed").

## The flyout (fixed 360 × 480, always on top)

Top to bottom:

1. **Header** — wordmark; status pill (`● live` green / `idle` grey /
   `offline` red); icon buttons: Refresh, Pin, Settings.
2. **Range tabs** — `Today · 7d · 30d · 90d`. Four tabs cover "right now",
   "this week-ish", "this month-ish" and "the quarter"; 24h lives on the mini
   overlay, and calendar week/month are what the report is for.
3. **Hero** — the range's cost, large. Sub-line: messages · tokens. On
   *Today* with a live session a second line shows `$X/h · <project>`.
4. **Sources line** — one chip per enabled source with its cost for the
   range (`Claude Code $12.40 · opencode $0.80`). A source that failed shows
   `Claude Code !` with the error as its tooltip — never a silent `$0.00`.
   Non-local sources report *today* only; on other tabs their chip reads
   `today only` so totals are never mixed silently.
5. **Bars** — Today: 24 hourly bars. Other tabs: one bar per day.
6. **Projects** and **Models** — top 5 each: name, cost, thin share bar.
7. **Footer** — `updated 12s ago` · `Report` button. When an update is
   available, an `Update to vX` button is added to the footer.

Settings open as a sheet over the flyout (see Settings). No context menu,
no shortcuts overlay, no toasts except "key saved / key invalid".

**Placement.** Always the bottom-right of the work area on the monitor
under the cursor, 12 px margin. Not draggable, not resizable. (Saved drag
positions could land off-screen or on a disconnected monitor — part of
"wrong position/size"; the resize grip fought WinForms autoscale.)

**Show / hide.** Tray click toggles. Esc hides. Focus loss hides unless
Pin is on. The tray process tracks visibility from the child's
`shown`/`hidden` events; the child hides the window itself and ignores a
blur within 400 ms of showing (existing settle guard, kept).

**Data refresh.** On every show: one `GET /api/today?refresh=1` (or the
range endpoint for the active tab). While visible: poll every 5 s with a
live session, 30 s without. While hidden: nothing (pushed
`window.__suprbarVisible(false)`; `document.hidden` is never trusted).
A failed request shows `offline` and retries with backoff 2 s → 30 s while
visible.

## Settings (5)

| Setting | Stored at | Live consumer |
|---|---|---|
| Sources on/off (6 toggles) + API keys (Anthropic, OpenRouter, OpenAI) with Test / Clear | `sources.<id>.enabled`, `sources.<id>.admin_key_enc` / `key_enc` | aggregator reads enabled sources per request; key saved → provider cache invalidated → next refresh shows it |
| Mini overlay | `mini.enabled` | tray `MiniController.sync()` starts/stops the overlay process immediately |
| Keep flyout open (pin) | `ui.pinned` | flyout child reads it on blur; pin button + tray middle-click toggle it |
| Start on login | `ui.start_on_login` | writes/removes the HKCU `Run` value immediately |
| Check for updates on launch | `updates.check_on_launch` | tray launch check; 6 h periodic check follows the same flag |

Internal (not shown): `updates.last_check`, `updates.skip_version`,
`schema_version`. Mini position stays in `window-state.json`
(`mini_x`/`mini_y`); flyout position/size keys are ignored.

Every change applies on toggle (no Save button) and is written only by the
tray process (`POST /api/settings`).

## Tray

- Menu: `Open supr.bar` (default, bold) · `Mini overlay` ✓ · `Refresh` ·
  `30-day report` · `Settings…` · `Quit`. When an update is available an
  extra `Update to vX…` item appears above Quit.
- Left-click toggles the flyout; double-click counts as one click;
  middle-click toggles pin.
- Icon: gradient S; green dot while a session is live. No budget tint.
- Tooltip: today's cost, messages, live project — unchanged.

## Mini overlay

Chip (176 × 44): live dot, rolling-24h cost, `$X/h` while live. Hover
expands to the detail card (today cost, messages, live project). Click
opens the flyout; × disables the overlay (`mini.enabled = false`). Polls
`/api/today` every 5 s live / 30 s idle; the process only exists while
enabled. No range toggle, no budget bar, no click-through.

## HTTP API (tray process, 127.0.0.1)

Kept / new:

| Route | Used by |
|---|---|
| `GET /`, `/app.js`, `/styles.css`, `/mini.html`, `/mini.js`, `/mini.css`, `/brand/*` | pages |
| `GET /api/today[?refresh=1]` | flyout, mini |
| `GET /api/range?key=7d\|30d\|90d[&refresh=1]` | flyout |
| `GET /api/settings` | flyout settings sheet (enabled flags, key fingerprints, the 4 toggles, version) |
| `POST /api/settings` | flyout settings sheet (dotted-path values and/or `{"keys": {"anthropic_api": "sk-…"}}`; empty string clears a key) |
| `POST /api/settings/test-key` | flyout (`{"source": "openrouter", "key": "…"}`) |
| `POST /api/quit` | tray Quit path (kept for the updater) |
| `GET /api/version` | flyout footer, install verification |
| `GET /report`, `GET /api/report`, `POST /api/open-report` | report page, flyout Report button |
| `GET /api/update/status`, `POST /api/update/check`, `POST /api/update/apply` | flyout update button |
| `GET /api/ping` | liveness |

Removed: `/api/budgets`, `/api/config` (GET/POST), `/api/config/test-key`,
`/api/config/export|import|reset`, `/api/prefs` (GET/POST),
`/api/prefs/schema`, `/api/open-path`, `/api/health`, `/api/diagnostics`.

The origin check, CSP and gzip behaviour stay as they are.

## Removed features

Budgets (limits, alerts, notifications, tray tint, per-project caps);
CSV export; copy summary; config export/import/reset; diagnostics;
shortcuts overlay; right-click menu; flyout and mini click-through;
confirm-quit; theme/accent/density/font-scale/animations/cost-format;
project allow/deny/anonymize/top-N; editable pricing URL (the built-in
hosted table and `pricing.local.json` still apply); default range and
week-start; live threshold (fixed 60 s); log level (fixed INFO; the
`SUPRBAR_LOG` env var stays for debugging); always-on-top toggle (always
on top); refresh interval (fixed 5 s / 30 s); flyout drag + resize; the
`24h`, `week`, `month` flyout tabs; the mini's today/24h toggle.

## Code changes

- `suprbar/static/index.html`, `app.js` — rewritten. Target: `app.js`
  ≤ 450 lines, `index.html` ≤ 120 lines.
- `suprbar/static/styles.css` — keep the dark tokens and the indigo
  accent; delete the light theme, the four other accent ramps, density /
  font-scale / motion variants, context menu, dialog, shortcuts, budget and
  settings-control styles no longer used. Target ≤ 450 lines.
- `suprbar/static/mini.js` / `mini.html` / `mini.css` — trimmed to the
  chip + card above.
- `suprbar/config.py` — DEFAULTS and SCHEMA cut to the table above;
  `SCHEMA_VERSION = 5` with a v4→v5 migration; helpers for removed
  settings deleted.
- `suprbar/server.py` — routes as above; budget, diagnostics, health,
  export/import/reset, prefs-schema, open-path code deleted.
- `suprbar/tray.py` — menu as above; budget alert/tint code deleted.
- `suprbar/scanner.py` — `budgets_summary` deleted; allow/deny/anonymize
  parameters removed from callers (the scanner functions keep working
  without them).
- `suprbar/popup.py` — fixed placement, no drag/resize persistence, no
  click-through; `resize_window`/`apply_click_through`/`apply_mini` JS API
  removed.
- `suprbar/mini.py` — click-through removed.
- `suprbar/pricing.py` — remote URL becomes a module constant.
- Tests for removed features deleted; the rest updated.

## Config migration (v4 → v5)

Kept verbatim: `sources.*` (every `enabled`, `admin_key_enc`, `key_enc`),
`mini.enabled`, `ui.start_on_login`, `updates.check_on_launch`,
`updates.last_check`, `updates.skip_version`.
Merged: `ui.pinned = ui.pinned or (behavior.auto_hide is False)` — whoever
turned auto-hide off gets a flyout that stays open.
Dropped: every other key. `config.json.bak` is written before the first
v5 save (existing `save()` behaviour). Unknown keys never crash loading.

## Stability fixes that ride along

- **Quit always exits.** The tray's shutdown stops both children (they get
  5 s, then are killed), stops the icon, and a 6 s watchdog `os._exit`s the
  tray process if anything hangs.
- **No duplicate instances.** Unchanged mutex + stale-instance cleanup,
  which never targets the current instance's own window children.
- **Numbers.** Server cache TTL never exceeds the poll interval (4 s live /
  25 s idle); a source error is reported per source; the date rollover
  test proves today resets at local midnight.
- **Blank flyout.** The child shows its window only after pywebview's
  `loaded` event on first show (the dark `background_color` covers the gap
  until then); later shows are instant.

## Error handling

- Source provider raises → that source's chip shows `!` + message; the
  total is the sum of the sources that worked.
- `/api/*` unreachable from a page → `offline` pill, backoff retry, no
  toast storm.
- Key test failure → inline `invalid key` / `network error` under the field.
- Child window process dies → the next tray click starts a new one.
- Corrupt `config.json` → defaults, warning logged (existing).

## Tests

- `test_config_migration.py` — v4 → v5 keeps sources/keys/mini/login/
  updates, merges pin, drops the rest; v5 loads unchanged; unknown keys ok.
- `test_server_settings.py` (new) — `GET /api/settings` shape (no plaintext
  keys), `POST /api/settings` applies each of the 5 settings and rejects
  unknown paths, removed routes return 404.
- `test_scanner_rollover.py` (new) — today's totals reset across a local
  midnight; the per-day index result for a whole-day range equals an exact
  scan of the same files (fixture JSONL in a temp dir).
- Existing aggregator / opencode / openrouter / pricing tests kept green;
  `test_ui_smoke.py` updated to the new page ids.

## Verification (before release)

1. pytest, ruff, mypy and `mypy --platform linux` green.
2. Source instance (`SUPRBAR_FORCE=1`) driven via WM_NOTIFY posts and the
   child-process harness: open / close / toggle / double-click / middle-click
   pin / idle exit after 60 s / mini on / mini off / each setting changes
   behaviour immediately. Pin during tests when the screen is locked;
   restore afterwards.
3. Flyout, settings sheet and report rendered in the browser pane from the
   local server at 360 × 480 and screenshotted.
4. `/api/today` and `/api/range` totals for today/7d/30d/90d equal the
   v0.16 scanner's on the same data.
5. Idle: one process, ≤ ~55 MB private. Flyout open measured. No ERROR
   lines in `%APPDATA%\suprbar\*.log`.
