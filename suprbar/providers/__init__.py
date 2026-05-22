"""Data-source providers for supr.bar.

Each provider implements `today_summary()` returning a dict shaped like:
    {
      "id": "local" | "anthropic_api" | ...,
      "label": "Claude Code · local",
      "ok": bool,
      "error": str | None,
      "cost_today": float,        # USD
      "tokens_today": {           # all keys present, may be 0
          "input": int, "output": int,
          "cache_5m": int, "cache_1h": int, "cache_read": int,
      },
      "messages_today": int,      # best effort, 0 if unknown
      "extras": dict,             # source-specific (e.g. active session)
    }
"""
