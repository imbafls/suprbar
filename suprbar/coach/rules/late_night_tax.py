"""Rule: after-hours sessions ship less.

Fires when an active session starts late evening. The body cites the
user's own 7-day pattern when there's enough signal.
"""

from __future__ import annotations

from datetime import timezone

from suprbar.coach.rule import Observation, Rule


class LateNightTax(Rule):
    id = "late-night-tax"
    name = "After-hours session"
    description = "Active session began after 22:00 local time."

    LATE_HOUR_START = 22  # >= 22:00 local
    LATE_HOUR_END   = 5   # < 05:00 local

    def evaluate(self, ctx) -> Observation | None:
        if not ctx.has_active_session or not ctx.session.started_at:
            return None
        local = ctx.session.started_at.astimezone()
        h = local.hour
        is_late = (h >= self.LATE_HOUR_START) or (h < self.LATE_HOUR_END)
        if not is_late:
            return None

        # Build optional cite from rolling.by_hour. Only used as flavor —
        # the rule fires on the time-of-day signal alone.
        late_cost = sum(ctx.rolling.by_hour[self.LATE_HOUR_START:]) \
                  + sum(ctx.rolling.by_hour[:self.LATE_HOUR_END])
        day_cost  = sum(ctx.rolling.by_hour)
        late_share = (late_cost / day_cost) if day_cost > 0 else 0.0

        cite = ""
        if late_share > 0.2:
            cite = (f" Your last 7 days had {late_share*100:.0f}% of spend "
                    f"between {self.LATE_HOUR_START:02d}:00 and "
                    f"{self.LATE_HOUR_END:02d}:00.")

        return Observation(
            id=self.id,
            severity="info",
            confidence=0.65,
            title="After-hours session",
            body=(
                f"This session started at {local.strftime('%H:%M')} local. "
                "Late sessions tend to cost more per shipped change because "
                "fatigue inflates rewrite ratios." + cite
            ),
            tip="Set a hard stop. If it's not done by then, shelf and resume tomorrow.",
        )
