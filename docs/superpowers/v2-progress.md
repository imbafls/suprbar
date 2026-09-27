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
- [ ] Write `docs/superpowers/plans/2026-09-27-suprbar-v2.md` (ordered small tasks + verification) and append its tasks to section 3 below

## 3. Implement
(tasks are added from the plan)

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
