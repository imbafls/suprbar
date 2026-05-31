# Extending supr.bar

supr.bar is a **usage bar** — tray flyout + local data sources. Extension
points are intentionally small:

1. **Data sources** — plug in Cursor, Codex CLI, OpenAI, etc.
2. **Themes / accents** — CSS variable overrides via `[data-theme]` / `[data-accent]` in `styles.css`.
3. **Tray menu actions** — optional HTTP-backed menu items (future).

---

## 1. Adding a data source

Each provider lives in `suprbar/providers/` and implements `today_summary()`:

```python
# suprbar/providers/my_tool.py
from typing import Any

def today_summary() -> dict[str, Any]:
    return {
        "id": "my_tool",
        "label": "My Tool · local",
        "ok": True,
        "error": None,
        "cost_today": 0.0,
        "tokens_today": {
            "input": 0, "output": 0,
            "cache_5m": 0, "cache_1h": 0, "cache_read": 0,
        },
        "messages_today": 0,
        "extras": {},  # optional: active session, project list, etc.
    }

def self_test() -> dict[str, Any]:
    return {"ok": True, "detail": "stub"}
```

Wire it up in `suprbar/aggregator.py`:

1. Import the module.
2. Add a config toggle under `sources.my_tool.enabled` in `config.py` DEFAULTS + SCHEMA.
3. Append to `_enabled_sources()` and the `today()` merge loop.

The flyout reads `/api/today` — once your source is in `sources[]`, it
appears in the per-source breakdown automatically.

For range queries (7d / 30d tabs), implement `range_summary(range_key)` on
your scanner adapter or reuse patterns from `suprbar/scanner.py`.

---

## 2. Themes & accents

There is no separate theme-file loader. Light/dark live in `styles.css` under
`[data-theme="light"]`, and the accent palette under `body[data-accent="…"]`.
To add an accent:

1. Add the name to the `display.accent` enum in `config.py` `SCHEMA`.
2. Add a matching block in `suprbar/static/styles.css`:

```css
body[data-accent="ocean"] {
  --b-accent: #38bdf8; --b-accent-hot: #7dd3fc; --b-violet: #6366f1;
}
```

It then appears as a swatch under Settings → Display → Accent color.

---

## 3. Provider contract (reference)

| Field | Type | Notes |
|---|---|---|
| `id` | str | Stable key, e.g. `local`, `anthropic_api` |
| `label` | str | Shown in UI chips |
| `ok` | bool | False → show error pill |
| `error` | str \| None | Human-readable failure |
| `cost_today` | float | USD equivalent or actual |
| `tokens_today` | dict | All five token buckets, may be 0 |
| `messages_today` | int | Best effort |
| `extras` | dict | Source-specific payload |

See `suprbar/providers/local.py` and `anthropic_api.py` for full examples.

---

## Planned: per-source filters

The flyout already shows a per-source breakdown (provider status cards). A
future rework may add a source picker (All / Claude Code / Anthropic API / …)
and per-source **project filters**. New providers should expose
`extras.projects` or similar so the UI can filter without re-scanning.
