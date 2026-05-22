# supr.bar CHANGELOG

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
