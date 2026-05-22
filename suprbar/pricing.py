"""Anthropic per-token rates (USD per million tokens).

Cache multipliers: 5-minute write = 1.25x input, 1-hour write = 2x input,
cache read = 0.1x input. Update these as Anthropic adjusts pricing.

Two rate dicts:
  * MODEL_RATES — exact model-id keyed (e.g. ``claude-opus-4-7``). Wins if
    the incoming model string normalizes to one of these keys.
  * PRICING — per-family fallback (opus / sonnet / haiku) when we can't
    match an exact model id.

Special suffix ``[1m]`` (1M-context tier) doubles the input rate per
Anthropic's published premium for the long-context variant.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------- rates ----

# Per-family fallback rates, USD per 1M tokens.
PRICING: dict[str, dict[str, float]] = {
    "opus":   {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0,  "output": 15.0},
    "haiku":  {"input": 1.0,  "output": 5.0},
}

# Per-exact-model rates. Keys are lower-cased, stripped of the
# ``claude-`` prefix and any date / 1m suffix (see _normalize_model_id).
# Values may include an optional ``input_1m`` rate for the 1M-context
# variant; if absent we apply 2x the regular input rate.
MODEL_RATES: dict[str, dict[str, float]] = {
    # Opus
    "claude-opus-4-7":  {"input": 15.0, "output": 75.0},
    "claude-opus-4-6":  {"input": 15.0, "output": 75.0},
    "claude-opus-4-5":  {"input": 15.0, "output": 75.0},
    "claude-opus-4-1":  {"input": 15.0, "output": 75.0},
    "claude-opus-4":    {"input": 15.0, "output": 75.0},
    "claude-opus-3":    {"input": 15.0, "output": 75.0},
    # Sonnet
    "claude-sonnet-4-7": {"input": 3.0,  "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0},
    "claude-sonnet-4-5": {"input": 3.0,  "output": 15.0},
    "claude-sonnet-4":   {"input": 3.0,  "output": 15.0},
    "claude-sonnet-3-7": {"input": 3.0,  "output": 15.0},
    "claude-sonnet-3-5": {"input": 3.0,  "output": 15.0},
    # Haiku
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0},
    "claude-haiku-4":    {"input": 1.0,  "output": 5.0},
    "claude-haiku-3-5":  {"input": 0.80, "output": 4.0},
    "claude-haiku-3":    {"input": 0.25, "output": 1.25},
}

CACHE_5M_MULT = 1.25
CACHE_1H_MULT = 2.0
CACHE_READ_MULT = 0.1

# 1M-context tier input premium (output unchanged).
ONE_M_INPUT_MULT = 2.0


# ---------------------------------------------------------------- helpers ----

def family_for(model: str) -> str:
    """Map a model id to its family. Robust to case + version suffixes.

    Tests for ``opus`` / ``sonnet`` / ``haiku`` substrings case-insensitively.
    Falls back to ``opus`` when nothing matches (preserves prior behavior).
    """
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "opus"


def _has_1m_suffix(model: str) -> bool:
    """True if the model string carries the 1M-context tier marker."""
    if not model:
        return False
    m = model.lower()
    return "[1m]" in m or m.endswith("-1m") or "_1m" in m or ":1m" in m


def _normalize_model_id(model: str) -> str:
    """Strip date/1m markers from a model id for MODEL_RATES lookup.

    Examples:
        ``claude-opus-4-7[1m]``             -> ``claude-opus-4-7``
        ``claude-opus-4-5-20250101``        -> ``claude-opus-4-5``
        ``Claude-Sonnet-4-6-20251015-Beta`` -> ``claude-sonnet-4-6``
    """
    if not model:
        return ""
    m = model.lower().strip()
    # remove explicit 1m markers
    for marker in ("[1m]", ":1m"):
        m = m.replace(marker, "")
    # strip trailing date suffix like "-20250101" or "_20250101"
    parts = m.replace("_", "-").split("-")
    out: list[str] = []
    for p in parts:
        if len(p) == 8 and p.isdigit():
            break
        # skip obvious tier labels appended after the version
        if p in {"beta", "preview", "1m"}:
            continue
        out.append(p)
    return "-".join(out)


def _rates_for(model: str) -> tuple[dict[str, float], bool]:
    """Resolve (rates, is_1m) for a model id.

    Returns the family fallback when the model id isn't in MODEL_RATES so
    callers always get a usable rate dict.
    """
    is_1m = _has_1m_suffix(model)
    norm = _normalize_model_id(model)
    if norm in MODEL_RATES:
        return MODEL_RATES[norm], is_1m
    fam = family_for(model)
    return PRICING[fam], is_1m


def rate_for_model(model: str) -> dict[str, float]:
    """Public: return effective {input, output} rate for a model id.

    Applies the 1M-context input premium when the model carries it.
    """
    rates, is_1m = _rates_for(model)
    inp = rates["input"] * (ONE_M_INPUT_MULT if is_1m else 1.0)
    return {"input": inp, "output": rates["output"]}


# ---------------------------------------------------------------- cost ----

def _extract_cache_tokens(usage: dict) -> tuple[int, int, int]:
    """Pull (cache_5m_create, cache_1h_create, cache_read) from a usage blob."""
    cache_create = usage.get("cache_creation") or {}
    c5 = cache_create.get("ephemeral_5m_input_tokens", 0) or 0
    c1 = cache_create.get("ephemeral_1h_input_tokens", 0) or 0
    if not (c5 or c1):
        c5 = usage.get("cache_creation_input_tokens", 0) or 0
    cr = usage.get("cache_read_input_tokens", 0) or 0
    return int(c5), int(c1), int(cr)


def cost_for(family_or_model: str, usage: dict[str, Any]) -> float:
    """Compute USD cost for one usage record.

    Accepts either a family name (``opus``/``sonnet``/``haiku``) for
    backward compatibility OR a full model id; in the latter case we
    use the more specific per-model rate.
    """
    rates, is_1m = _rates_for(family_or_model)
    # If caller passed a bare family name, _rates_for falls through to
    # PRICING[family] — same as the old behavior.
    if family_or_model in PRICING and not is_1m:
        rates = PRICING[family_or_model]

    inp_rate = rates["input"] * (ONE_M_INPUT_MULT if is_1m else 1.0)
    out_rate = rates["output"]

    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    c5, c1, cr = _extract_cache_tokens(usage)
    return (
        inp * inp_rate
        + out * out_rate
        + c5 * inp_rate * CACHE_5M_MULT
        + c1 * inp_rate * CACHE_1H_MULT
        + cr * inp_rate * CACHE_READ_MULT
    ) / 1_000_000


def cache_savings_for(usage: dict[str, Any], model: str = "") -> float:
    """Approximate dollars saved by cache reads (vs. uncached input).

    Anthropic charges ~10% of the input rate for cache-read tokens, so each
    cache-read token saved ~90% of full input cost — multiply by 0.9.
    """
    _, _, cr = _extract_cache_tokens(usage)
    if not cr:
        return 0.0
    rate = rate_for_model(model) if model else PRICING["opus"]
    return (cr * rate["input"] * 0.9) / 1_000_000


# ---------------------------------------------------------------- self-test ----

if __name__ == "__main__":
    # Lightweight assertions — run via ``python -m suprbar.pricing``.
    assert family_for("claude-opus-4-7") == "opus"
    assert family_for("Claude-Opus-4-7-20250101") == "opus"
    assert family_for("claude-sonnet-4-6[1m]") == "sonnet"
    assert family_for("CLAUDE-HAIKU-4-5") == "haiku"
    assert family_for("") == "opus"  # default fallback
    assert family_for("mystery-model") == "opus"

    assert _normalize_model_id("claude-opus-4-7[1m]") == "claude-opus-4-7"
    assert _normalize_model_id("claude-opus-4-5-20250101") == "claude-opus-4-5"
    assert _normalize_model_id("Claude-Sonnet-4-6-Beta") == "claude-sonnet-4-6"

    assert _has_1m_suffix("claude-opus-4-7[1m]") is True
    assert _has_1m_suffix("claude-opus-4-7") is False
    assert _has_1m_suffix("claude-sonnet-4-6-1m") is True

    # Per-model rate beats family fallback
    rate = rate_for_model("claude-haiku-3-5")
    assert rate["input"] == 0.80, f"haiku-3-5 input should be 0.80, got {rate['input']}"

    # 1M premium doubles input
    rate_1m = rate_for_model("claude-opus-4-7[1m]")
    assert rate_1m["input"] == 30.0, f"opus 1m input should be 30.0, got {rate_1m['input']}"
    assert rate_1m["output"] == 75.0

    # cost_for still accepts family strings (backward compat)
    c = cost_for("opus", {"input_tokens": 1_000_000, "output_tokens": 0})
    assert abs(c - 15.0) < 1e-9, f"opus 1M input should be $15, got {c}"

    # cost_for accepts model ids and respects 1m premium
    c1m = cost_for("claude-opus-4-7[1m]", {"input_tokens": 1_000_000, "output_tokens": 0})
    assert abs(c1m - 30.0) < 1e-9, f"opus 1m 1M input should be $30, got {c1m}"

    # Cache read costs 10% of input
    cr_cost = cost_for("opus", {"input_tokens": 0, "cache_read_input_tokens": 1_000_000})
    assert abs(cr_cost - 1.5) < 1e-9, f"opus 1M cache_read should be $1.50, got {cr_cost}"

    # Cache savings ~ 90% of full input
    saved = cache_savings_for({"cache_read_input_tokens": 1_000_000}, "claude-opus-4-7")
    assert abs(saved - 13.5) < 1e-9, f"opus 1M cache savings should be $13.50, got {saved}"

    print("pricing.py self-test OK")
