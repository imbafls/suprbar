# Contributing to supr.bar

Thanks for considering a contribution. The project is small on purpose and
the surface area for contributors is intentionally narrow: **most PRs should
be new rules.**

## What we welcome

| Type | Effort | Priority |
|---|---|---|
| **New rule** in `suprbar/coach/rules/` | small | ★★★★★ |
| **Bug report** with a reproducer JSONL snippet | small | ★★★★ |
| **New theme** in `suprbar/static/themes/` | small | ★★★ |
| **Docs fix** | tiny | ★★★ |
| **New data source** (Codex CLI / Cursor / Gemini / etc.) | medium | ★★ |
| **Refactor** of existing code | large | ★ (please open an issue first) |

## What we'll politely decline

- Telemetry of any kind.
- Required cloud sync.
- "AI-generated advice" — observations must be deterministic and explainable.
- New top-level dependencies without a strong reason; we want a small
  install footprint.
- Re-styling the existing UI without a visual rationale (open an issue with
  before/after first).

## Setup

```sh
git clone https://github.com/omertaji/suprbar
cd suprbar
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
pip install -r requirements-dev.txt   # ruff, pytest, mypy

# run the app
python -m suprbar

# run tests
pytest

# lint + types
ruff check .
mypy suprbar
```

The only required tools are Python 3.11+ and a WebView2 runtime
(preinstalled on Win11). No Node, no build step.

## Writing a rule

This is the contribution that helps most. Full guide:
[`docs/extending.md`](./docs/extending.md).

Quick version:

1. Create `suprbar/coach/rules/<your_id>.py` (snake_case).
2. Subclass `Rule`. Implement `evaluate(self, ctx) -> Observation | None`.
3. Add a test in `tests/coach/rules/test_<your_id>.py` that feeds the rule
   a hand-crafted `SessionContext` and asserts the observation it should
   emit.
4. Open a PR. The CI runs `pytest`, `ruff`, and `mypy`.

A good rule:

- Fires sparingly — false positives erode trust faster than missed signals.
- Returns `confidence < 0.5` if it's a hunch; the UI suppresses those.
- Has a concrete `tip` if the user can actually do something about it.
- Has a stable `id`; the user's mute-list is keyed on it.

## Commit + PR conventions

- Branch name: `rule/<id>`, `theme/<name>`, `fix/<short>`, `docs/<short>`.
- Commit subject: imperative present tense, ≤ 72 chars. Body is optional.
- One logical change per PR. We squash on merge.
- Reference an issue number when one exists.
- Include a screenshot if the change touches visible UI.

## Code style

- Python: `ruff` defaults + type hints on every public function.
  No `Any` unless you justify it in a comment.
- CSS: keep custom properties at the top of `styles.css`. Avoid inline
  styles in HTML.
- JS: vanilla. No frameworks. No build step. No external runtime deps.
- Names: snake_case for Python files and identifiers; kebab-case for CSS
  classes; camelCase for JS local identifiers.

## License grant

By submitting a PR, you agree that your contribution is licensed under the
MIT license that covers the rest of the project, and that you have the
right to grant that license.

## Code of conduct

Be decent. Engineering disagreements are welcome and expected. Personal
attacks, harassment, or "well actually" gatekeeping are not — and PRs from
people who do that will be closed without further discussion.

## Where to ask

- Discussions: [github.com/omertaji/suprbar/discussions](https://github.com/omertaji/suprbar/discussions)
- Issues: bugs and concrete feature requests only
- Anything sensitive (security report, conduct concern): email the address
  in the repo `SECURITY.md`
