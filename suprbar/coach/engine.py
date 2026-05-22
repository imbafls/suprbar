"""Coach engine — discover rules, evaluate them, pick the hero.

Rules are auto-discovered from ``suprbar/coach/rules/*.py`` (project) and
``~/.suprbar/rules/*.py`` (user). User rules override shipped rules of
the same id.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import logging
from pathlib import Path
from typing import Any

from .. import config
from .context import SessionContext, build_context
from .rule import Observation, Rule, SEVERITY_WEIGHT

log = logging.getLogger("suprbar.coach.engine")

_RULES_CACHE: dict[str, Rule] | None = None


def _shipped_rules_dir() -> Path:
    return Path(__file__).parent / "rules"


def _user_rules_dir() -> Path:
    home = config.local_data_dir().parent  # %LOCALAPPDATA% root
    # Prefer ~/.suprbar/rules so it's symmetric with sessions/templates.
    return Path.home() / ".suprbar" / "rules"


def _import_module_from_path(path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001 - rule import errors must not crash app
        log.warning("rule import failed %s: %s", path, e)
        return None
    return mod


def discover_rules(force: bool = False) -> dict[str, Rule]:
    """Return {rule.id: Rule()} for all known rules, user-overriding-shipped."""
    global _RULES_CACHE
    if _RULES_CACHE is not None and not force:
        return _RULES_CACHE

    rules: dict[str, Rule] = {}

    def harvest(dir_path: Path, name_prefix: str):
        if not dir_path.exists():
            return
        for f in sorted(dir_path.glob("*.py")):
            if f.name.startswith("_"):
                continue
            mod = _import_module_from_path(f, f"{name_prefix}.{f.stem}")
            if mod is None:
                continue
            for _, obj in inspect.getmembers(mod, inspect.isclass):
                if obj is Rule or not issubclass(obj, Rule):
                    continue
                if not obj.id:
                    continue
                try:
                    rules[obj.id] = obj()
                except Exception as e:  # noqa: BLE001
                    log.warning("rule init failed %s: %s", obj.id, e)

    harvest(_shipped_rules_dir(),    "suprbar_rules_shipped")
    harvest(_user_rules_dir(),       "suprbar_rules_user")

    log.info("coach: %d rule(s) registered → %s",
             len(rules), ", ".join(sorted(rules.keys())))
    _RULES_CACHE = rules
    return rules


def _muted_ids() -> set[str]:
    """Per-rule mute list from config (`coach.muted_rules: list[str]`)."""
    cfg = config.load()
    coach = cfg.get("coach", {}) or {}
    return set(coach.get("muted_rules") or [])


def all_observations(ctx: SessionContext | None = None) -> list[Observation]:
    """Evaluate every rule. Return all fired observations, sorted by score."""
    if ctx is None:
        ctx = build_context()
    rules = discover_rules()
    muted = _muted_ids()
    out: list[Observation] = []
    for rid, rule in rules.items():
        if rid in muted:
            continue
        try:
            obs = rule.evaluate(ctx)
        except Exception as e:  # noqa: BLE001 - one bad rule must not crash
            log.warning("rule %s crashed: %s", rid, e)
            continue
        if obs is None:
            continue
        out.append(obs)
    out.sort(key=_score, reverse=True)
    return out


def _score(o: Observation) -> float:
    return o.confidence * SEVERITY_WEIGHT.get(o.severity, 1.0)


def run(ctx: SessionContext | None = None) -> dict:
    """Build the payload the popup consumes."""
    obs = all_observations(ctx)
    hero = obs[0] if obs else None
    minor = [o for o in obs[1:] if o.confidence >= 0.5]
    return {
        "hero": hero.to_dict() if hero else None,
        "more": [o.to_dict() for o in minor],
        "rule_count": len(discover_rules()),
        "muted_count": len(_muted_ids()),
    }
