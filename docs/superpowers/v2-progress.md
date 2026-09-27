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
- [ ] T5 Config schema v5 + migration (tests first)
- [ ] T6 Settings API (tests first), remove old routes
- [ ] T7 Tray menu trim + clean quit
- [ ] T8 New flyout page (index.html + app.js)
- [ ] T9 Trim styles.css
- [ ] T10 Trim the mini overlay
- [ ] T11 Blank-flyout guard
- [ ] T12 Scanner rollover + index equality tests
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
