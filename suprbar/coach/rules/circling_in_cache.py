"""Rule: you're iterating in circles.

Fires when the active session is heavy on cache reads relative to fresh
input — usually a sign you're re-prompting against the same context
instead of moving forward. The model is busy re-reading what it already
knows.
"""

from __future__ import annotations

from suprbar.coach.rule import Observation, Rule


class CirclingInCache(Rule):
    id = "circling-in-cache"
    name = "Iterating in circles"
    description = "Cache-read share dominates fresh input on a long live session."

    CACHE_RATIO_THRESHOLD = 0.70
    MIN_MESSAGES = 20

    def evaluate(self, ctx) -> Observation | None:
        if not ctx.has_active_session:
            return None
        if ctx.session.messages_today < self.MIN_MESSAGES:
            return None
        ratio = ctx.today.cache_hit_ratio
        if ratio < self.CACHE_RATIO_THRESHOLD:
            return None

        confidence = min(
            1.0,
            0.5 + (ratio - self.CACHE_RATIO_THRESHOLD) * 2.0,
        )
        return Observation(
            id=self.id,
            severity="nudge",
            confidence=confidence,
            title="You're iterating in circles",
            body=(
                f"{ratio*100:.0f}% of input is being served from cache, with "
                f"{ctx.session.messages_today} messages in this session. "
                "The model is re-reading the same context instead of making "
                "progress on a new sub-goal."
            ),
            tip=(
                "Stop, write one sentence describing what 'done' looks like, "
                "and paste it as a new turn — preferably in a fresh session."
            ),
        )
