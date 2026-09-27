# supr.bar v2.0 — progress

Approach B (agreed with Omer 2026-09-27): rebuild the flyout small, trim the
backend, keep the Python core, the process model and the visual design.
One item per loop iteration, in order; tick only with evidence.

## 0. Setup
- [x] Commit the pending tray double-click / middle-click fix — `tray.py` now
  patches pystray's `_message_handlers[WM_NOTIFY]`; verified on a source
  instance via WM_NOTIFY posts (dbl-click opens/closes, middle-click pins).

## 1. Spec
- [x] Write `docs/superpowers/specs/2026-09-27-suprbar-v2-design.md` and self-review it — spec covers screen, 5 settings + consumers, tray, 16 routes kept / 11 removed, v4→v5 migration, errors, tests, verification; placeholder scan clean

## 2. Plan
- [x] Write `docs/superpowers/plans/2026-09-27-suprbar-v2.md` (ordered small tasks + verification) and append its tasks to section 3 below — 13 tasks, each with its own check + the pytest/ruff/mypy×2 gate

## 3. Implement
- [x] T1 Remove budgets — scanner.budgets_summary, /api/budgets, tray tint+alerts, report budget card, config.project_limit_map and 3 tests deleted; grep clean outside config schema; report payload builds; gate green (20 passed, ruff, mypy×2)
- [x] T2 Remove project filters, anonymize, top-N — params gone from scanner/server/report, config helpers deleted (schema keys go in T5); 7d/30d/90d totals differ from baseline by exactly today's live growth (+8 msgs / +$5.9471 each); gate green
- [x] T3 Fixed flyout placement, no click-through — flyout placed in physical px with the monitor's DPI (was logical-on-physical, off-screen on scaled displays); no drag/resize/saved pos/click-through anywhere; harness: window rect (1548,660,360,480) == flyout_rect() on show, re-show and after toggle; gate green
- [x] T4 Fixed internals (pricing URL, log level, live threshold, range prefs, cache TTL) — REMOTE_URL constant (refresh_remote(force)=True), INFO/SUPRBAR_LOG logging, 60 s live window shared by scanner+opencode, range/report ignore range prefs, idle today TTL 25 s < 30 s poll; 5 config accessors deleted; grep clean; gate green
- [x] T5 Config schema v5 + migration (tests first) — 6 new tests written first (5 failed), now pass; SCHEMA = 12 paths (6 sources, mini, pin, login, 3 update); copy of real config.json: v4→v5, sources incl. key blobs identical, 5 top-level keys; gate green (25 passed)
- [x] T6 Settings API (tests first), remove old routes — tests/test_server_settings.py (6 tests, written first, errored) now pass: shape w/o plaintext keys, each setting persists, login applied, mini callback fires, bad paths 400, key set/clear, test-key source check, 13 removed routes 404; old config/prefs/health/diagnostics/open-path code + 5 config helpers deleted; gate green (29 passed)
- [x] T7 Tray menu trim + clean quit — build_menu(): Open, Mini overlay, Refresh, 30-day report, Settings…, Quit (+ hidden-unless-available Update); Pin/About/Check-for-updates gone; children stop in parallel + 6 s os._exit watchdog; periodic check obeys updates.check_on_launch; source instance with flyout child quit via /api/quit in 1.1 s, 0 leftover descendants; gate green
- [x] T8 New flyout page (index.html + app.js) — app.js 2,677→362 lines, index.html 89, fresh styles.css 237 (tokens kept for mini); browser pane 360×480 on a source server: Today (live pill, $411.95, burn, chips, hourly bars, projects/models) and 30d ($78,460, daily bars, 'today only' chips) render, settings sheet renders 6 sources + 4 toggles, a toggle persisted via re-GET (restored), 0 console errors; only kept routes used; smoke test updated; gate green
- [x] T9 Trim styles.css — rewritten in T8 (859→237 lines, target ≤450: dark tokens + indigo only; light theme, 4 accent ramps, density/font/motion, context menu, dialogs, shortcuts, budget and old settings styles gone); audit script: 0 rules without a user, 0 page classes without a rule, 0 unused / 0 undefined tokens (flyout + mini); no CSS change since T8's screenshots
- [x] T10 Trim the mini overlay — mini.js 228→82, mini.css 176→47, mini.html 55→33; no prefs/budget/range/localStorage; browser pane: chip 176×44 ($455.2 · $200.0/h) and card 208×118 (project · $/h, 24h cost, 'last 24h · N msgs', 'today $X', no overflow); harness: mini.enabled on → window visible 176×44, off → process exit 0; setting restored; smoke test updated; gate green
- [x] T11 Blank-flyout guard — before: cold window shown at +0.80 s, page's first /api/today at +1.05 s (blank flash); after: first show waits for pywebview `loaded` (≤3 s): /api/today +1.02 s, shown +1.11/+1.12 s on two runs; toggle/re-show still correct; gate green
- [x] T12 Scanner rollover + index equality tests — tests/test_scanner_rollover.py: today resets across local midnight (pinned clock, cached scanner), 8-day whole-day index == exact scan (totals, sessions, projects, by_model, by_project); mutation checks: no date reset → 2≠1 fails, off-by-one index window → 12≠14 fails; gate green (33 passed)
- [ ] T13 UI smoke test + dead-code sweep

## 4. Verify
- [ ] pytest, ruff, mypy (default + `--platform linux`) green
- [ ] Source run: flyout open/close/toggle/idle-exit, mini on/off, settings changes (harness + tray WM_NOTIFY)
- [ ] Browser screenshots of flyout, settings, report at 360x480 look right
- [ ] Numbers match the v0.16 scanner for today / 7d / 30d / 90d
- [ ] Idle memory (1 process, <= ~55 MB) and flyout-open memory measured; no errors in logs

## 5. Docs
- [ ] README (features, status v2.0), CHANGELOG v2.0.0, memory file updated if the model changed

## 6. Release
- [ ] Version 2.0.0 in `__init__.py`, `pyproject.toml`, `installer.iss`; commit; push main
- [ ] Tag v2.0.0, push tag; Release + CI workflows green (fix forward if not)

## 7. Install
- [ ] Download + checksum-verify the installer, quit the running app, install silently, `/api/version` = 2.0.0, memory measured
