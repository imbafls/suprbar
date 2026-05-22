"""Rule: nothing's wrong.

Always fires at very low confidence. The engine surfaces it only when
no other rule produced anything — so the hero slot is never empty when
the user opens the popup.
"""

from __future__ import annotations

from suprbar.coach.rule import Observation, Rule


class SteadyState(Rule):
    id = "steady-state"
    name = "Steady state"
    description = "Fallback observation when nothing else fires."

    def evaluate(self, ctx) -> Observation | None:
        if ctx.has_active_session:
            body = (
                f"{ctx.session.messages_today} messages, "
                f"${ctx.session.cost_today:,.2f} so far, "
                f"{ctx.today.cache_hit_ratio*100:.0f}% cache. "
                "No patterns to flag — keep going."
            )
            title = "All clear"
        else:
            body = (
                f"No live session. Today: {ctx.today.messages} messages "
                f"across {ctx.today.sessions_today} session(s), "
                f"${ctx.today.cost:,.2f} equivalent."
            )
            title = "Idle"
        return Observation(
            id=self.id, severity="info",
            confidence=0.1,  # always last in sort order
            title=title, body=body, tip=None,
        )
