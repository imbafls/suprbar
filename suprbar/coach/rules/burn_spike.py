"""Rule: today's burn is well above your norm.

Surfaces when today's equivalent-cost is multiples of the 7-day median.
The number itself isn't a value judgement — sometimes big features cost
more — but it's useful to *know* you're in an unusual day.
"""

from __future__ import annotations

from suprbar.coach.rule import Observation, Rule


class BurnSpike(Rule):
    id = "burn-spike"
    name = "Burn spike"
    description = "Today's spend is well above the 7-day median."

    MULTIPLIER = 3.0       # today must be ≥ N× the median to fire
    MIN_MEDIAN = 1.0       # require a meaningful baseline ($1 equiv min)

    def evaluate(self, ctx) -> Observation | None:
        median = ctx.rolling.median_cost_per_day
        today = ctx.today.cost
        if median < self.MIN_MEDIAN or today <= 0:
            return None
        ratio = today / median
        if ratio < self.MULTIPLIER:
            return None
        # Saturate confidence around 5×.
        confidence = min(1.0, 0.6 + (ratio - self.MULTIPLIER) * 0.1)
        return Observation(
            id=self.id,
            severity="info",
            confidence=confidence,
            title=f"Today's spend is {ratio:.1f}× your 7-day median",
            body=(
                f"You're at ${today:,.2f} today vs a $${median:,.2f} typical "
                f"day this week. Could be a big feature, could be drift — "
                "worth naming which before you push on."
            ),
            tip=None,
        )
