"""Anthropic per-token rates (USD per million tokens).

Cache multipliers: 5-minute write = 1.25x input, 1-hour write = 2x input,
cache read = 0.1x input. Update these as Anthropic adjusts pricing.
"""

PRICING = {
    "opus":   {"input": 15.0, "output": 75.0},
    "sonnet": {"input": 3.0,  "output": 15.0},
    "haiku":  {"input": 1.0,  "output": 5.0},
}

CACHE_5M_MULT = 1.25
CACHE_1H_MULT = 2.0
CACHE_READ_MULT = 0.1


def family_for(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    return "opus"


def cost_for(family: str, usage: dict) -> float:
    p = PRICING[family]
    inp = usage.get("input_tokens", 0) or 0
    out = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_create = usage.get("cache_creation") or {}
    c5 = cache_create.get("ephemeral_5m_input_tokens", 0) or 0
    c1 = cache_create.get("ephemeral_1h_input_tokens", 0) or 0
    if not (c5 or c1):
        c5 = usage.get("cache_creation_input_tokens", 0) or 0
    return (
        inp * p["input"]
        + out * p["output"]
        + c5 * p["input"] * CACHE_5M_MULT
        + c1 * p["input"] * CACHE_1H_MULT
        + cache_read * p["input"] * CACHE_READ_MULT
    ) / 1_000_000
