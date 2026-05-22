"""Rule: hour N — diminishing returns.

Fires on long live sessions. The body scales the title with how long
you've been at it.
"""

from __future__ import annotations

from suprbar.coach.rule import Observation, Rule


class TimeToStop(Rule):
    id = "time-to-stop"
    name = "Long session"
    description = "Active session has run for more than 3 hours."

    HOUR_THRESHOLD = 3.0
    WARN_HOUR_THRESHOLD = 5.0

    def evaluate(self, ctx) -> Observation | None:
        if not ctx.has_active_session:
            return None
        h = ctx.session.duration_hours
        if h < self.HOUR_THRESHOLD:
            return None

        severity = "warn" if h >= self.WARN_HOUR_THRESHOLD else "nudge"
        confidence = min(1.0, 0.55 + (h - self.HOUR_THRESHOLD) * 0.1)
        # nice human duration
        hh = int(h)
        mm = int((h - hh) * 60)
        dur = f"{hh}h {mm}m" if mm else f"{hh}h"

        return Observation(
            id=self.id,
            severity=severity,
            confidence=confidence,
            title=f"You've been at this for {dur}",
            body=(
                "Sessions past three hours typically produce more rewrites "
                "per shipped commit. The marginal turn is rarely the one "
                "that ships it."
            ),
            tip="Snapshot your state, commit a checkpoint, and break.",
        )
