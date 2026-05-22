"""supr.bar coach — observations, not counters.

See pivot_v1.md for the design. The coach watches the active session and a
short rolling history, then surfaces a single highest-confidence observation
to the popup. Rules live in suprbar/coach/rules/*.py and are auto-discovered.
"""

from .rule import Observation, Rule  # noqa: F401
from .context import SessionContext   # noqa: F401
from .engine import run as run_engine, all_observations  # noqa: F401
