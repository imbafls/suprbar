# supr.bar CHANGELOG

## v0.10.2 — updater download-host fix

- **Critical updater fix.** GitHub serves release-asset downloads from
  `release-assets.githubusercontent.com`, but the updater's host allowlist only
  had `objects.githubusercontent.com`, so the per-redirect-hop host check
  rejected the real download and **"Update" failed** with *"redirect to
  disallowed host"*. The allowlist now accepts any GitHub-owned
  `*.githubusercontent.com` host (look-alikes still rejected), validated end to
  end against a live release. v0.10.0 / v0.10.1 can *detect* an update but can't
  download it — **install this build manually once**, and auto-update works from
  here on.

## v0.10.1 — updater shakedown

- **First release that exercises the in-app auto-updater end to end** (v0.10.0 →
  v0.10.1). On the installed v0.10.0 build, the flyout shows an "Update
  available — v0.10.1" banner (and the tray an "Update to v0.10.1…" item); one
  click downloads, verifies, and installs this.
- **Tidies up after itself.** The apply path keeps its download dir while the
  installer runs, so leftover `suprbar_upd_*` folders are now swept from the temp
  dir on launch (anything older than 24h).

## v0.10.0 — in-app auto-update

supr.bar can now update itself. No more checking Releases by hand — when a new
build ships, the flyout tells you, and one click pulls it down and installs it.

### Checks for new releases
- **Once per launch**, in the background, supr.bar asks GitHub for the latest
  release and — if it's newer than what you're running — surfaces it. You can
  also check **on demand**: the **Updates** action in the flyout footer, **Check
  now** in About, or **Check for updates** in the tray menu.
- When an update is found you get an **accent-toned banner** in the flyout
  (*"Update available — v0.10.0"* with **Update** / dismiss), a tray
  notification, and a dynamic tray item **"Update to vX…"**. Dismissing a
  version hides the banner for that version only.

### One-click install
- **Update** downloads the installer asset straight from the GitHub release,
  validates it, launches it silently, and restarts into the new version — no
  manual download, unzip, or re-run.
- The download is hardened: HTTPS-only with a host allowlist (re-checked after
  the GitHub redirect), an installer-name allowlist
  (`suprbar-setup-X.Y.Z.exe`), SHA-256 verification against the release asset's
  digest (exact-size fallback), and a size ceiling. Any failure aborts cleanly
  and leaves your install untouched — it never half-applies.

### Local-first and honest
- **No telemetry.** The only network call is an **unauthenticated `GET`** for
  the latest-release version — no account, no token, nothing about you is sent
  (just a `suprbar/<version>` User-Agent). Everything else stays on your machine.
- **Opt out** anytime: turn off **Settings → Updates → Check on launch**
  (`updates.check_on_launch`) and supr.bar never reaches out on its own; manual
  checks still work.
- **Not code-signed (yet).** This build isn't code-signed, so Windows
  **SmartScreen may warn** when the installer runs. That's expected — choose
  **More info → Run anyway**. Running from a source checkout never auto-updates
  (update via `git` instead).

## v0.9.2 — 30-day usage report export

A new **HTML-based usage report** — a beautiful, self-contained, printable
spend report covering the last 30 days, built from the same local `~/.claude`
scan that drives the flyout. Integrated from a Claude Design handoff. (This is
the first published build of the report — the v0.9.0 release was deleted; the
installer-label and report-XSS fixes below are folded in here.)

### The report
- Open it from the tray menu (**"30-day report…"**) or the flyout **Report**
  button — both launch the full report in your real browser (the flyout popup
  is only ~360px wide; the report wants room).
- **Glance-down-the-page** layout: a hero 30-day total with a *vs previous 30
  days* delta, KPI row (messages / tokens / sessions / active days / avg-day /
  cache saved), a daily-spend bar chart with weekday/weekend/peak shading and an
  average line, monthly-budget gauge, **by-model** and **top-projects**
  breakdowns, a token-mix + cache-efficiency panel, and a few derived highlights.
- **Download HTML** gives a fully self-contained file you can keep or share
  offline; **Save as PDF** prints it. Dark/light + all five accents, same token
  system as the app.

### Honest by default
- v1 is **local-only** (Claude Code) and says so — one source card, footer reads
  "Read from Claude Code (local) — supr.bar sends no telemetry." The payload is
  list-shaped so a future multi-source merge is a drop-in.
- No monthly budget set → the budget section shows a calm "no cap set" state
  instead of a fake gauge. No `~/.claude` history → a zero-state, not a blank page.

### Under the hood
- `report.build_report()` assembles the payload from `range_summary("30d")` plus
  a previous-30-day comparison, honoring your project allow/deny filters.
- New routes `GET /report`, `GET /api/report`, `POST /api/open-report`; the
  payload is cached (30s) and invalidated with the existing today/range caches,
  so refreshes don't re-run the disk scan.
- Disk-derived strings (project names, model ids) are HTML-escaped and the
  injected JSON is `</`-escaped, so the report stays safe under its scoped
  inline-script CSP.

### Fixes folded in
- **Installer label/metadata now tracks the release.** The first installer asset
  was mislabeled `suprbar-setup-0.8.0.exe` (with 0.8.0 metadata) because
  `installer.iss` hardcoded the version as a separate source of truth. The build
  now injects the version — from the git tag in CI and from `suprbar.__version__`
  for local builds — with the `installer.iss` literal demoted to a fallback, so
  the installer name and metadata always track the release. The app and portable
  zip were already correct; this only affected the installer's label/metadata.
- **Report XSS hardening.** Escape the by-project model label (`p.model`) before
  inserting it into the report DOM. `_model_family()` passes raw model ids
  through for any model that isn't opus/sonnet/haiku, so a crafted local
  `~/.claude` transcript `message.model` could execute under `/report`'s
  inline-script CSP. Completes the output-escaping above (every other
  disk-derived string was already escaped). Caught by automated PR review.

## v0.8.0 — glance-first redesign

A full visual redesign, integrated from a Claude Design handoff. The flyout was
busy — a wall of numbers you had to scroll. This rebuilds it **glance-first**:
the one thing you open it 50× a day to learn — *what am I spending, will I blow
budget today* — is answered in one eyeful, with everything else folded away.

The data, behavior, hooks, settings, and shortcuts are unchanged. Only the look
and the information hierarchy changed.

### New visual system
- **Cool neutral near-black base** (`#0b0c0e`) with a refined **indigo** accent
  (the new default). Five accents ship; all route through a single `--b-accent`
  via `oklch` + `color-mix`, so switching accent recolours the entire UI — token
  bar, project bars, budget fill, focus rings, chips — with no hardcoded hex.
- **System font stack** (Segoe UI) for UI, **tabular mono** for every number;
  the hero `$` is mono with de-emphasized cents. No remote fonts.
- Dark + light themes, density (compact/normal/spacious), and font-scale
  (0.85×–1.25×) all drive off CSS variables.

### Glance-first information architecture
- The active popup fits hero → live + projected → budget → "Now burning" in
  360×480 with **no scroll**.
- **"Now burning" card** promotes the live session actually spending money into
  a focal object (project · model · $cost · msgs · burn $/h).
- **Budget "fuel gauge"** — `$spent / $limit`, bar, and `$X left` with a derived
  **"on pace to go over by $Y"** line (computed from today's projected spend vs
  the daily limit; no new data).
- **Projected hero signal** — `▲ projected $X`, amber when projected spend would
  exceed the daily budget.
- Everything secondary (token mix, hourly sparkline, metric grid, other live
  sessions, source cards, top projects) folds behind a **Details** disclosure —
  the only state that scrolls.

### Polish
- Settings overlay restyled to the new system (quick chips, section nav,
  swatches, sliders, tag inputs) — same schema-driven engine.
- Tray icon retinted to the indigo gradient (`#5b8fe8 → #7a6cf0`), 22% corner
  radius, live status dot `#2bd07a`.
- Toast, context menu, shortcuts dialog, and tooltips moved onto the new tokens.

## v0.7.0 — lean & honest (100 improvements)

The app drifted into 74 settings — about half of them switches that did
nothing. This release cuts the dead weight, wires up the toggles worth keeping,
fixes the bugs that audit turned up, and removes the code no one calls. Every
visible setting now does something; the only network dependency (Google Fonts)
is gone; and the tray icon finally matches the terracotta flyout.

### Simplified the settings surface (74 → 42, every one wired)
1. Removed `range.compare_previous` (labeled "planned", did nothing).
2. Removed `range.custom_start` (no custom-range UI exists).
3. Removed `range.custom_end` (same).
4. Trimmed the `range.default` enum to the seven real tabs (dropped `yesterday`/`custom`, added `week`/`month`).
5. Removed `display.currency` (cosmetic; app is USD-only).
6. Removed `display.locale` (never applied to number formatting).
7. Removed `budgets.audio_alert` (no audio path).
8. Removed `budgets.quiet_hours` (no alert scheduler).
9. Removed `budgets.quiet_start`.
10. Removed `budgets.quiet_end`.
11. Removed `behavior.show_in_taskbar` (popup is always a tool window).
12. Removed `behavior.start_minimized` (boot is always tray-only).
13. Removed `behavior.single_instance` (the mutex is unconditional).
14. Removed `behavior.open_dashboard_on_click` (tray click is hardcoded).
15. Removed `keyboard.enable_global` (global hotkeys never implemented).
16. Removed `keyboard.hotkey_toggle`.
17. Removed `keyboard.hotkey_refresh`.
18. Removed `keyboard.hotkey_settings`.
19. Removed `keyboard.hotkey_quit`.
20. Removed `keyboard.hotkey_export`.
21. Removed `keyboard.hotkey_copy_cost`.
22. Removed `keyboard.vim_keys` — the whole **Keyboard** section is gone (shortcuts are fixed in the client).
23. Removed `data.log_retention_days`.
24. Removed `data.anonymize_logs`.
25. Removed `data.cache_ttl_seconds`.
26. Removed `data.telemetry` — honoring the README's "no telemetry" promise.
27. Removed `window.anchor`.
28. Removed `window.margin_px`.
29. Removed `window.preferred_monitor`.
30. Removed `window.remember_position`.
31. Removed `window.opacity`.
32. Removed `sources.cost_mode` (aggregator never branched on it).
33. Removed `sources.anthropic_api.poll_seconds` (cadence is fixed).
34. Added a schema-v3 migration that prunes removed keys from existing configs on load.
35. The migration normalizes a now-invalid saved `range.default` back to `today`.

### Made half-built toggles actually work
36. `display.cost_format` now switches the hero between cents and whole dollars.
37. `display.token_format` now switches token counts between `1.2k` and `1,234`.
38. `display.show_model` now hides the Model tile.
39. `display.show_project` now hides the footer project.
40. `display.show_sessions_today` now hides the Sessions tile.
41. `budgets.notify` now pops a toast when a budget crosses its threshold/limit.
42. Display changes repaint the flyout instantly instead of waiting for the next poll.

### Bug fixes
43. **Manual refresh no longer hammers the server** — `setInterval(load, 0)` spun a ~4 ms loop; "0 = manual" now actually stops polling.
44. Tray refresh loop had identical `if/else` branches; collapsed and removed the dead source-change probe.
45. Range view could render literal `undefined sess · undefined proj` — guarded.
46. Space-to-refresh stole activation from focused buttons/tabs/links — now bails on interactive controls.
47. `body.offline .cost-num` targeted a class that doesn't exist — fixed to `.cost`, so the offline dim works.
48. Duplicate `.btn-sm.primary` left the Export button stale blue — removed; it's terracotta now.
49. Cache-savings estimate charged **all** cache reads at the priciest model's rate — now attributed per model.
50. Single-instance check ran *after* the HTTP server started, leaking the socket/thread on a duplicate launch — moved to the top of `main()`.
51. Admin-API `User-Agent` was hardcoded `suprbar/0.1` — now derived from `__version__`.
52. Corrected the swapped app.js header note (Ctrl+L opens logs, Ctrl+K focuses key).
53. Today CSV's session count used a 1–2 heuristic — now uses `insights.sessions_today`.

### Removed dead backend code
54. Deleted `scanner.scan()` (~70 lines, no callers).
55. Deleted its `scanner._empty_scan()` helper.
56. Deleted its `scanner._proj_totals()` helper.
57. Deleted `aggregator._now_iso()` (unused).
58. Removed the unused `/api/projects` route and `_projects_payload`.
59. Removed the unused `/api/sources` route and `_sources_payload`.
60. Removed the unused `/api/sources/{id}` route and `_source_by_id`.
61. Removed the unused `GET /api/window-state` route.
62. Removed the unused `POST /api/window-state` route.
63. Removed the duplicate window-state implementation from `config.py` (popup owns it).
64. Removed now-orphaned `config.local_data_dir()`.
65. Removed the dead `config.click_through()` accessor.
66. Removed `server.try_bind()` (no longer called).
67. Removed `import socket` from `__main__.py`.
68. Removed the redundant port-ping single-instance path and its `urllib` imports.

### Removed dead frontend code
69. Deleted `renderSourcesPanel()` (drove a `#sourcesPanel` that doesn't exist).
70. Deleted three click handlers for DOM nodes that were removed (`#anthropicToggle`/`#pinnedToggle`/`#startupToggle`).
71. Deleted the orphaned `setToggle()` / `toggleValue()` helpers.
72. Cleaned `loadConfig()` of references to removed nodes.
73. Pointed `applyTabOrder()` at controls that still exist.
74. Dropped a write to a non-existent toggle in `runTestKey`.
75. Pruned 32 stale entries from the settings `LABELS` map.
76. Removed `keyboard` from the settings nav/order and its render special-case.

### Removed dead / duplicate CSS
77. Deleted unused `.settings-section-title`.
78. Deleted unused bare `.settings-section`.
79. Deleted unused `.settings-section[data-section]`.
80. Deleted duplicate `.settings-section.empty-section`.
81. Deleted legacy `.settings-actions`.
82. De-duplicated the split `body.compact` block.

### Brand consistency (terracotta everywhere)
83. Tray icon gradient switched from leftover blue/violet to the flyout's terracotta.
84. Focus-ring glow recolored from stale blue to the accent.
85. Empty-state glyph glows recolored to terracotta.
86. Project-bar gradient recolored to terracotta.
87. Tag-chip / tag-input highlight recolored to the accent.
88. Settings-search focus border recolored to the accent.
89. Input / select / range-thumb focus states recolored to the accent.

### Offline & security
90. Removed the Google Fonts `<link>` tags — fully offline on the system font stack.
91. Tightened the CSP to drop the `fonts.googleapis.com` / `fonts.gstatic.com` allowances.

### Accessibility & UX
92. Range tabs carry `aria-selected` at load, so a screen reader sees the active tab.
93. Added a `#mModelCell` wrapper so the Show-model toggle hides the tile cleanly.

### Diagnostics
94. Wired each provider's `self_test()` into `/api/diagnostics` (per-source health + last error).
95. Surfaced per-model `cache_read` from the scanner (feeds the accurate savings estimate).

### Docs & release
96. Bumped `__version__` 0.6.0 → 0.7.0.
97. Bumped installer `MyAppVersion` 0.6.0 → 0.7.0 (keeps the asset name in step with the tag).
98. README: status → v0.7, de-duplicated the roadmap, range list matches the real tabs.
99. Fixed the non-existent `suprbar/static/themes/` guidance (extending.md + CONTRIBUTING) and stale "v0.5 adds…" tense; corrected `build_brand.py`'s output-path docstring.
100. Refreshed module docstrings (scanner/server/config) and the app.js header to match the trimmed surface.

## v0.6.0 — usage command center

- Adds impact insights: projected spend, average cost per message, cache savings,
  live count, top-project share, and parse-error state.
- Adds flyout polish: insight chips, hourly sparkline, provider status cards,
  ranked project bars, live glow, and richer live-session rows.
- Adds usability upgrades: copy summary, active session opener, range persistence,
  1-7 range shortcuts, arrow-key range navigation, safer pinned auto-hide, and
  clearer footer actions.
- Adds a regression test for backend insight calculations.

## v0.5.1 — exe startup fix

- Fix PyInstaller entry point (`suprbar_main.py`) so the Windows exe no longer
  crashes with `ImportError: attempted relative import with no known parent package`.
- Bundle now collects all `suprbar` submodules explicitly.

## v0.5.0 — live session scan

- Scans all Claude Code JSONL sessions touched within the live window (not just one).
- Flyout **Live now** panel: project, model, cost, burn rate per active session.
- Tray indicator shows `N live` when multiple sessions are running.
- Drag performance fixes, refresh button, coach layer removed (usage bar focus).
- Windows installer + portable zip via GitHub Release on tag.

## v0.4 — coach experiment (reverted)

**Direction change.** supr.bar pivots from a passive usage counter to an
active coach. See `pivot_v1.md` for the full plan.

- New `suprbar/coach/` package: `Rule`, `Observation`, `SessionContext`,
  `engine.py`, six built-in rules.
- Session retros: idle → `~/.suprbar/sessions/<date>.md`.
- Settings panel collapses from 74 entries to roughly 12.
- Range tabs, budget UI, and project allow/deny UI are removed; their
  backend stays for reuse.
- New brand & docs: `BRAND.md`, `README.md` (rewritten),
  `CONTRIBUTING.md`, `docs/extending.md`, `docs/index.html`, MIT
  `LICENSE`, issue templates.

## v0.3 — 74 settings + range filters + budgets

(Counter-era peak. Kept here for history; most of this UI is deliberately
removed in v0.4.)

- Schema-driven settings (74 entries, 10 sections, search, validation)
- Range filters: today / 24h / 7d / week / month / 30d / 90d / custom
- Budgets endpoint + tray-icon budget palette
- Server-side range cache + client cache + boot prefetch

## v0.2 — 100 improvements across 5 parallel agents

5 subagents dispatched in parallel, each owning a strict file domain (no
overlap → no merge conflicts). Each landed 20 improvements; one bonus
(connection-error handling) makes 101.

### Data layer (scanner, pricing, aggregator, providers — commit `6f43cb1`)
1. Per-model pricing dict (opus/sonnet/haiku × 4.x / 3.x variants) with 1M-context premium
2. Burn rate ($/hr) on active session
3. Cache hit ratio (cache_read / total_input)
4. Cache savings (USD saved vs uncached input)
5. Distinct projects today count
6. Top model today
7. Per-project today breakdown
8. Session count today
9. 24-hour hourly breakdown
10. Incremental scan cache (mtime+size keyed)
11. Parallel JSONL scan (ThreadPoolExecutor)
12. Fast line skip (substring pre-filter)
13. parse_errors counter (graceful malformed-JSONL)
14. Robust family_for() + self-test (`python -m suprbar.pricing`)
15. Admin API retry w/ exponential backoff (0.5s/1s/2s)
16. Admin API 25s timeout w/ clean reason text
17. Per-provider self_test() diagnostics
18. updated_at per source
19. Cache key fingerprint includes admin-key fp
20. cache_meta surfaced at top level

### Server / control plane (server, config, __main__ — commit `6f43cb1`)
21. ETag on /api/today + 304 If-None-Match
22. Gzip compression (Accept-Encoding gated, ≥1024B)
23. GET /api/diagnostics
24. GET /api/version
25. GET /api/projects
26. GET /api/sources + /api/sources/{id}
27. RotatingFileHandler logs at `%APPDATA%\suprbar\suprbar.log`
28. GET /api/config/export
29. POST /api/config/import (rejects plaintext keys)
30. POST /api/config/reset (preserves admin key by default)
31. Standardized error envelope
32. Input validation on POST /api/config (allow-list)
33. GET /api/health
34. Schema versioning (config schema_version=1 + migrate)
35. Atomic config save with .bak backup
36. time.monotonic() for cache TTL
37. GET/POST /api/window-state (geometry persistence)
38. Single-instance enforcement (port + ping; SUPRBAR_FORCE override)
39. Cleaner shutdown (SIGINT handler + try/finally)
40. CSP + X-Content-Type-Options nosniff headers

### Frontend JS (commit `47065ec`)
41. Count-up cost animation (rAF 400ms ease-out)
42. Toast notification system
43. ? shortcut shows help dialog
44. Ctrl+L focuses admin key
45. Ctrl+E CSV export
46. Ctrl+W closes popup
47. F5 / Ctrl+R refresh w/ visual indicator
48. Click cost number to copy
49. Tab order management in settings
50. Auto-focus admin key on settings open
51. Enter in admin key field triggers Test
52. Right-click context menu
53. Per-source view in settings
54. Visibility-aware polling (5s active / 60s hidden)
55. Auto-retry w/ exponential backoff + connection-lost toast
56. Status-code-aware error messages
57. Multi-day fmtDuration
58. AbortController for in-flight /api/today
59. Settings save toast (success + failure)
60. Skeleton loading class removal on first render

### Frontend HTML/CSS (commit `dd1fc7c`)
61. Focus-visible rings (a11y)
62. Refined hover states w/ subtle transforms
63. Smooth cost transition (color/opacity)
64. Pin button rotates 45deg when on
65. Toast markup + CSS variants
66. Loading skeleton (.loading body class)
67. Tabular-nums on all numerics
68. Source-pill color variants (local=claude orange, api=accent blue)
69. Custom scrollbar (cross-browser)
70. Settings overlay slide animation polish
71. Compact mode (.compact body class)
72. Empty-state graphic with glow rings
73. 4th metric tile (Burn) — auto-fit grid
74. Cache-hit % indicator chip
75. Top-projects collapsible list (`<details>`)
76. [data-tip] tooltip system
77. <dialog> shortcuts help
78. Theme variables ([data-theme="light"] ready)
79. Disabled states (toggle / btn / icon / input)
80. ARIA & semantic HTML (real buttons, aria-pressed, aria-live)

### Tray / popup / Windows (commit `f1551f3`)
81. Multi-monitor positioning (MonitorFromPoint)
82. Window position memory (%LOCALAPPDATA%\suprbar\window-state.json)
83. Live vs idle tray icon variants (green dot overlay)
84. DWM Mica backdrop (DWMSBT_TRANSIENTWINDOW)
85. Multi-line tooltip layout (≤250 chars)
86. Tray menu — Settings (#settings hash + JsApi)
87. Tray menu — About (notify w/ version)
88. Refresh-now icon pulse
89. WebView2 missing-runtime MessageBox
90. hwnd cached via loaded event (no more poll-by-title)
91. Single-instance mutex (Global\suprbar-single-instance)
92. Double-click tray opens popup
93. Middle-click tray toggles pin
94. 30s refresh + source-change detection
95. Crisp icon (256→64 LANCZOS)
96. Anti-aliased "S" w/ Segoe UI Black + alpha-composite overlay
97. Snap-to-corner on drag end (within 24px)
98. Restore-position clamped to current monitor work area
99. Force-redraw on display change
100. Graceful shutdown (destroy + webview.shutdown + mutex release)

### Post-merge fix (commit `<this commit>`)
101. server: catch ConnectionAbortedError + OSError on wfile.write
