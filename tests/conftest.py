"""Pytest configuration — make the suite hermetic w.r.t. config_overrides.json.

`config.py` loads `config_overrides.json` from a `__file__`-relative path at import
time (the `BotConfig` singleton, config.py:481/508). On the production box that file
exists, so an *in-place* `pytest` run reads LIVE values (drawdown 0.28, breadth 20,
per-market cap 60, A/B on, trial pct 0.75, fill window 900, ...) and ~23
config-sensitive tests that assert repo-DEFAULT behaviour fail spuriously. They are
NOT regressions — the live override is leaking into the test process. (This is why the
box gate previously needed a clean-worktree workaround; see
docs/MASTER_PLAN_2026-06-22.md A-1 and the `repo_config_defaults_not_live` memory.)

This autouse fixture snapshots → clears → restores `BotConfig`'s override layer per
test, so every test reads `config.py` DEFAULTS regardless of any live
`config_overrides.json` in the repo dir:
  - Box in-place `pytest` now gates cleanly (no worktree dance).
  - Laptop / CI (no override file): `_overrides` is already empty, the clear is a no-op.
  - Tests that set their OWN overrides (tests/test_ab_cohorts.py `cfg_overrides`,
    tests/test_merge_cost_accounting.py) still work: this fixture clears at setup, the
    test sets its keys in the body, and teardown (LIFO) restores. `_defaults` is never
    touched, so default values are intact.

Non-behavioral: test-harness only; no production code path is affected.
"""

import pytest

from config import BotConfig


@pytest.fixture(autouse=True)
def _hermetic_config_overrides():
    bc = BotConfig.instance()
    saved = dict(bc._overrides)
    bc._overrides.clear()
    try:
        yield
    finally:
        bc._overrides.clear()
        bc._overrides.update(saved)
