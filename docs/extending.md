# Extending supr.bar

supr.bar is a **usage bar** — tray flyout + local data sources. Extension
points are intentionally small:

1. **Data sources** — plug in Cursor, Codex CLI, OpenAI, etc.
2. **Themes** — CSS variable overrides in `suprbar/static/themes/`.
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

## 2. Themes

Drop a CSS file in `suprbar/static/themes/<name>.css`:

```css
[data-theme="ocean"] {
  --b-accent: #38bdf8;
  --b-violet: #6366f1;
}
```

Select it via Settings → Display → Theme (or add to the theme enum in
`config.py` SCHEMA).

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

## Planned: source tabs + filters

The v0.5 rework adds a **source picker** in the flyout (All / Claude Code /
Anthropic API / …) and optional **project filters** per source. New providers
should expose `extras.projects` or similar so the UI can filter without
re-scanning.
