# Polymarket Reward Farming Bot

[![Tests](https://github.com/sabyweb/Polymarket-bot/actions/workflows/test.yml/badge.svg)](https://github.com/sabyweb/Polymarket-bot/actions/workflows/test.yml)

Two-process bot that farms Polymarket liquidity rewards while remaining profitable. A `simple_oversight` planner plans every ~30 minutes; a `reward_farmer` executes every ~30 seconds. Capital, risk, and state flow between them via SQLite (WAL) and a single allocation JSON. (`oversight_agent.py` is the legacy/rollback planner — not the production path.)

## Documentation

- **`Polymarket bot architecture v5.1.md`** — system reference (states, invariants, allocator math, ops runbook).
- **`Polymarket bot fixit.md`** — living tracker of open issues, the FX-NNN backlog, and the phased hardening roadmap.

## Test tiers

- **Fast tier (CI-gated):** `pytest tests/ --ignore=tests/test_simulation.py --tb=short` — runs on every push to `main` and on every pull request via GitHub Actions.
- **Slow tier:** `tests/test_simulation.py` — long-running scenario sim, run manually.

## Runtime

- Python 3.14
- Polymarket SDK: `py-clob-client-v2==1.0.0`
- Production server: Hetzner Helsinki — P5 Stage-C **live bounded canary** (`--mode live`, ~5-market cap), re-launched 2026-06-01 with corrected reward/loss accounting (FX-088/089) after the first canary surfaced + fixed FX-087/088/089. Current live state + P&L: `docs/STATUS_2026-05-31.md` (Addendum 5). Not yet through G-C (real fill) / G-E (7-day clean); profitability pending the daily reward settlement (~00:20 UTC).
