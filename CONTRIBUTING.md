# Contributing to supr.bar

Thanks for considering a contribution. supr.bar is a **local-first usage bar**
(CodexBar-style for Windows). The highest-value contributions are new **data
sources** and bug fixes with reproducers.

## What we welcome

| Type | Effort | Priority |
|---|---|---|
| **New data source** in `suprbar/providers/` | medium | ★★★★★ |
| **Bug report** with a reproducer JSONL snippet | small | ★★★★ |
| **New accent / theme** (`config.py` enum + `styles.css` `[data-accent]`) | small | ★★★ |
| **Docs fix** | tiny | ★★★ |
| **UI polish** on the usage flyout | medium | ★★ |
| **Large refactor** | large | ★ (open an issue first) |

## What we'll politely decline

- Telemetry of any kind.
- Required cloud sync.
- Coach / nudge / "AI advice" features — out of scope.
- New top-level dependencies without strong justification.

## Setup

```sh
git clone https://github.com/imbafls/suprbar
cd suprbar
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
pip install -r requirements-dev.txt   # ruff, pytest, mypy

python -m suprbar
pytest
ruff check .
mypy suprbar
```

Python 3.11+ and WebView2 (Win11). No Node, no frontend build step.

## Adding a data source

See [`docs/extending.md`](./docs/extending.md). Implement `today_summary()`,
register in `aggregator.py`, add a config toggle.

## Commit + PR conventions

- Branch: `source/<id>`, `theme/<name>`, `fix/<short>`, `docs/<short>`.
- Commit subject: imperative, ≤ 72 chars.
- One logical change per PR; squash on merge.
- Screenshot for visible UI changes.

## License

MIT — by submitting a PR you license your contribution under the project MIT
license.
