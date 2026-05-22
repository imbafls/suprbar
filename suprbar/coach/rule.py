"""Coach Rule + Observation primitives.

A rule is a small class with a single ``evaluate(ctx)`` method. It returns
an Observation if the rule fires, or None if it should stay quiet. Rules
are pure functions of the context — no I/O, no global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Severity = Literal["info", "nudge", "warn"]


@dataclass(frozen=True)
class Observation:
    id: str            # stable id (matches Rule.id) — used for mute-list
    severity: Severity # "info" | "nudge" | "warn"
    title: str         # one short line, sentence case, no period
    body: str          # 1-3 sentence elaboration
    tip: str | None    # optional concrete next action
    confidence: float  # 0.0 - 1.0; UI suppresses below 0.5 unless severity=warn

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "body": self.body,
            "tip": self.tip,
            "confidence": round(self.confidence, 3),
        }


SEVERITY_WEIGHT: dict[str, float] = {"info": 0.6, "nudge": 1.0, "warn": 1.5}


class Rule:
    """Subclass and implement ``evaluate(self, ctx) -> Observation | None``.

    Class attributes provide metadata for the Settings → Coach panel and
    the mute-list keyed on ``id``.
    """

    id: str = ""
    name: str = ""
    description: str = ""

    def evaluate(self, ctx: "SessionContext") -> Observation | None:  # noqa: F821
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<Rule {self.id}>"
