# Extending supr.bar

There are five places where contributors can extend the product without
touching the core. From easiest to hardest:

1. **Rules** — observations the coach surfaces.
2. **Themes** — CSS variable overrides.
3. **Wrap templates** — the markdown shape of session retros.
4. **Data sources** — plug in non-Claude-Code session sources.
5. **Tray menu actions** — small HTTP-backed actions in the right-click menu.

Each one has a complete working example below.

---

## 1. Writing a rule

The rule is the heart of supr.bar. A rule is a tiny Python class that gets
a read-only view of the current session + recent history and returns an
optional `Observation`.

### File location

```
suprbar/coach/rules/<your_rule_id>.py       # ships with the project
~/.suprbar/rules/<your_rule_id>.py          # user-local, not committed
```

Both directories are scanned at boot. User-local rules can override
shipped rules of the same `id`.

### Skeleton

```python
# suprbar/coach/rules/long_message.py
from suprbar.coach.rule import Observation, Rule


class LongMessage(Rule):
    id = "long-message"
    name = "Long prompt"
    description = "Flags user messages over 4,000 characters."

    # Tunables — exposed in Settings → Coach as numeric inputs.
    CHAR_THRESHOLD = 4_000

    def evaluate(self, ctx) -> Observation | None:
        last = ctx.recent_user_messages(limit=1)
        if not last:
            return None
        msg = last[0]
        if len(msg.text) < self.CHAR_THRESHOLD:
            return None
        return Observation(
            id=self.id,
            severity="info",
            confidence=0.6,
            title="Long prompt",
            body=(
                f"Your last message was {len(msg.text):,} characters. "
                "Long prompts often hide multiple asks; the model picks one."
            ),
            tip="Split into two turns: state the constraint, then the change.",
        )
```

That's the whole API. No registration, no plugin manifest, no schema —
drop the file in, restart suprbar, the rule is live and toggleable in
Settings → Coach.

### `SessionContext` cheat sheet

```python
ctx.session                 # active session metadata
ctx.session.id              # uuid
ctx.session.project         # "C--depot--projects-suprbar"
ctx.session.model           # latest model used
ctx.session.started_at      # datetime
ctx.session.live            # bool

ctx.recent_user_messages(limit=5)         # → list[UserMessage]
ctx.recent_assistant_messages(limit=5)    # → list[AssistantMessage]
ctx.tool_calls(limit=20)                  # → list[ToolCall]

ctx.today.cost              # float, USD equivalent
ctx.today.messages          # int
ctx.today.cache_hit_ratio   # float 0..1

ctx.rolling(days=7).median_cost_per_hour
ctx.rolling(days=7).rewrite_ratio
ctx.rolling(days=7).by_hour(of_day=23).cost
ctx.rolling(days=7).model_mix          # {"opus": 0.7, "sonnet": 0.3}

ctx.git.commits_since(ctx.session.started_at)        # → list[Commit]
ctx.git.files_changed_since(ctx.session.started_at)  # → list[Path]
```

If you need a field the context doesn't expose, open an issue rather than
parsing the JSONL yourself — the context layer is what keeps rules stable
across upgrades.

### Severity

| Value | Used for | Visible when |
|---|---|---|
| `info`  | Neutral observations ("hour 4, ratio is...") | Always |
| `nudge` | Mild suggestion ("worth pausing") | Always |
| `warn`  | Strong signal ("stuck", "burning") | Always, optionally with one-shot sound |

Never `error` or `critical`. supr.bar isn't an alert system.

### Confidence

`Observation.confidence` is a float in `[0, 1]`. The engine sorts active
observations by confidence × severity weight and shows the top one in the
hero slot. Returning `confidence < 0.5` makes the observation a "minor
note" — visible in the more-notes drawer but never the hero.

### Testing a rule

```python
# tests/coach/rules/test_long_message.py
from suprbar.coach.rules.long_message import LongMessage
from tests.helpers import make_context

def test_long_message_fires_above_threshold():
    ctx = make_context(user_messages=["x" * 5000])
    obs = LongMessage().evaluate(ctx)
    assert obs is not None
    assert obs.id == "long-message"
    assert obs.severity == "info"

def test_long_message_quiet_below_threshold():
    ctx = make_context(user_messages=["short"])
    assert LongMessage().evaluate(ctx) is None
```

`tests/helpers.make_context()` is the canonical way to build a fake
`SessionContext` for unit tests. It returns a fully-typed object — no
mocks, no monkey-patching.

---

## 2. Themes

```
suprbar/static/themes/<name>.css
```

A theme overrides the `:root` custom properties. To ship one:

```css
/* suprbar/static/themes/solarized.css */
[data-theme="solarized"] {
  --bg-grad-top: #002b36;
  --bg-grad-bot: #001619;
  --surface:    rgba(7, 54, 66, 0.92);
  --text:       #93a1a1;
  --b-accent:   #268bd2;
  --b-violet:   #6c71c4;
}
```

Add the file, then add the theme name to `display.theme`'s enum in
`config.py`. The Settings → Display select picks it up automatically.

---

## 3. Wrap templates

The session retro is rendered with Jinja2. The default template lives at
`suprbar/wrap/templates/default.md.j2`. Drop a sibling template:

```jinja
{# ~/.suprbar/templates/minimal.md.j2 #}
# {{ session.project }} — {{ session.date }}

- {{ session.duration_human }} · {{ session.messages }} msgs · ${{ "%.2f" % session.cost }}
- Top file: {{ session.top_file or "—" }}
- {{ observations | length }} observations
{% for obs in observations %}
  - {{ obs.title }}
{% endfor %}
```

Then point `data.wrap_template` (`suprbar/coach/wrap.py` will resolve the
name) at `minimal`. v0.4 ships only `default`; the template loader is
present from day one so others can ship more.

---

## 4. Data sources

Local JSONL is the default. To add another source:

```python
# suprbar/providers/codex_local.py
from suprbar.providers.base import Source, SourceResult

class CodexLocal(Source):
    id = "codex_local"
    label = "OpenAI Codex (local CLI logs)"

    def today_summary(self) -> SourceResult:
        # parse ~/.codex/sessions/*.json or wherever Codex stores them
        ...

    def session_context(self):
        # optional: return a SessionContext shaped to fit the coach
        ...
```

Then enable the source in `config.py` defaults under `sources.codex_local`.

We will add Codex / Cursor / Gemini support post-v0.5 if there's demand.
Until then, the local Claude Code source is intentionally the only first-
party integration.

---

## 5. Tray menu actions

The tray right-click menu is small on purpose. To add an item, register a
callable in `suprbar/tray_actions/`:

```python
# suprbar/tray_actions/open_today_retro.py
from pathlib import Path
import os, sys

from suprbar.tray_actions import TrayAction, register

@register
class OpenTodayRetro(TrayAction):
    id = "open-today-retro"
    label = "Open today's retro"

    def run(self):
        p = Path.home() / ".suprbar" / "sessions" / f"{today_iso()}.md"
        if p.exists():
            os.startfile(str(p)) if sys.platform == "win32" else None
```

The action shows up in the tray menu and the right-click context menu in
the popup. The same registry powers HTTP routes at `/api/actions/<id>` so
keyboard shortcuts and external tools can fire them too.

---

## Bigger refactors

Open an issue first. The core (scanner / popup / config / engine) has
stable interfaces; we're happy to change them, but the change should be
discussed before someone spends a weekend on it.

## Style of new copy

Match the rule output guidance in
[`BRAND.md`](../BRAND.md#voice). Short, second-person, no emoji, no
exclamation marks, no moralising about cost.
