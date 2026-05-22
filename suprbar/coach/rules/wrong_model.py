"""Rule: cheap-job on an expensive model.

Fires when an Opus session is short and looks like it could have been
handled by Sonnet. Heuristic v0: few messages, low output volume, no
sign of deep reasoning.
"""

from __future__ import annotations

from suprbar.coach.rule import Observation, Rule


class WrongModelCheapJob(Rule):
    id = "wrong-model-cheap-job"
    name = "Wrong model for the job"
    description = "Opus session that looks like Sonnet work."

    MAX_MESSAGES = 6      # quick task
    MAX_OUTPUT_TOKENS = 5_000  # not a big-thinking session

    def evaluate(self, ctx) -> Observation | None:
        if not ctx.has_active_session:
            return None
        model = (ctx.session.model or "").lower()
        if "opus" not in model:
            return None
        if ctx.session.messages_today > self.MAX_MESSAGES:
            return None
        if ctx.today.output > self.MAX_OUTPUT_TOKENS:
            return None

        return Observation(
            id=self.id,
            severity="info",
            confidence=0.55,
            title="Sonnet would have done this",
            body=(
                f"{ctx.session.messages_today} message(s) on Opus with low "
                "output volume — looks like routine work. Opus is 5× the "
                "rate for the same answer on a quick task."
            ),
            tip="Try /model sonnet next time you're touching small tweaks.",
        )
