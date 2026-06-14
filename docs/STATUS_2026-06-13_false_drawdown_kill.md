# MORNING BRIEFING — farmer halt on 2026-06-13 (FALSE drawdown kill)

> Written overnight while you slept. Read this first. **Nothing is on fire; capital is safe.**
> I deliberately changed nothing on the live bot. Full root-cause detail is in
> `docs/POSTMORTEM_2026-06-12.md` (Update 2026-06-13b + sharpened FIX-3).

## Bottom line (corrected vs. the earlier chat)
The farmer is **halted on a FALSE drawdown kill.** It is **not** the "real 16% erosion" the earlier
session reported. **True portfolio ≈ $1,047 → 14.2% drawdown, UNDER the 15% limit.** Nothing was
lost in this event; the bot bought $22 of inventory and the *cash-only* drawdown metric mistook the
cash→inventory conversion for a loss. I did **not** restart, reset the baseline, or deploy anything
— resuming would put the bot back into the known loss-making regime at the drawdown edge while a
safety hole (RC-5/FIX-3) is still open. That trade-off is **your call, awake.**

## What actually happened (ground-truthed on-chain)
- 17:58:46 — bot bought **$22.17 of "JD Vance signs a U.S.×Iran deal" YES @0.47** (a maker fill). It
  still holds it: data-api `/value` = **$22.41**, pnl **+$0.24**.
- That fill was **never written to the DB** `fills`/`positions` tables (the kill fired ~35 s later;
  the farmer started skipping cycles before recording it).
- `simple_oversight.run_once` (line 272) sources inventory from the **DB** → empty dict →
  `compute_portfolio_value` returned **cash-only $1,024.57** → drawdown read **16.1% > 15%** → kill.
- True portfolio = cash $1,024.57 + inventory $22.41 = **$1,046.98 = 14.2% dd.** False trip.
- **It's deadlocked:** halted → skips cycles → never records the fill → metric stays cash-only →
  stays killed. **It will NOT auto-clear.**
- The watchdog is correctly paging this as a live kill (you may see overnight Discord/Telegram
  alerts — that's expected, not a second problem).

## Your decision (pick one)
**A — Leave it halted until FIX-3 lands (safest).** Cost: ~$9/day of foregone reward. The bot is at
the drawdown edge farming the RC-2 leak anyway, so "off" is not a bad place to be.

**B — Resume now, accepting the bot farms RC-2 at ~14% dd with the RC-5 hole open.**
```bash
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203 \
  'sudo systemctl restart polymarket-farmer && echo restarted'
# startup _sync_exchange_positions registers the $22 position → next OVERSIGHT cycle
# (~30 min) recomputes portfolio=$1047 → 14.2% → kill clears. Verify kill=false:
ssh -i ~/.ssh/polymarket_bot_ed25519 polymarket@46.62.209.203 \
  'journalctl -u polymarket-farmer --since "35 min ago" -o cat | grep CYCLE_SUMMARY | tail -2'
```
RISK: it re-enters the same regime; at 14.2% it could re-false-kill on the next fill, or take a real
loss the RC-5-blind metric under-fires on. **Not recommended unsupervised.**

**C — Fix the metric first (RECOMMENDED), then resume.** Deploy FIX-3 so the kill reads authoritative
on-chain value, not the stale DB. This both clears *this* false trip correctly and closes the RC-5
under-fire hole. Spec below. Per your locked plan it must pass the invariant gate + canary; but as a
*safety* fix it's the right next move (postmortem §11 now says prioritize FIX-3 ahead of FIX-2).

## FIX-3 — exact spec (ready to build)
- **Locus:** `simple_oversight.run_once`, line 272 — `positions, mids = _load_positions_and_mids(db_path)`.
- **Change (single axis):** for the **kill input only**, source inventory from the authoritative
  data-api `/positions` (same call the farmer's `_sync_exchange_positions` already uses) and feed
  that into `compute_portfolio_value`. Leave allocation logic untouched.
- **Fail-safe (critical):** if the data-api read fails/times out, **do not** silently fall to
  cash-only (that's the bug). Fall back to the *last good* portfolio value, or hold the prior
  kill-state — a missing reading must not falsely fire **or** falsely clear the kill.
- **Reversibility:** behind a flag (`RF_KILL_PORTFOLIO_SOURCE=onchain|db`, default `db`).
- **Success test:** replay 06-13 — the metric reads ~$1,047 (14.2%), no false kill; and replay
  06-12 — the −$72.58 held-to-resolution loss now registers (vs the $13.54 the DB showed).

## The $22 JD-Vance position
Fine — +$0.24, max downside ~$22 (1.5% of wallet). The bot currently doesn't track it (DB blind); a
restart's startup sync will register it. No action needed overnight.

## What I did / did NOT do overnight
- **DID:** ground-truthed on-chain (corrected the diagnosis); read the kill/portfolio/sync code;
  updated `POSTMORTEM_2026-06-12.md` (Update 06-13b + sharpened FIX-3 with the exact locus); wrote
  this briefing; ran read-only safety re-checks through the night.
- **Did NOT (deliberately):** restart the farmer, reset the drawdown peak, edit the live DB, or
  deploy any code. No live state was mutated. Helsinki remains at `71528de` (3 commits behind local;
  it does **not** yet have FIX-1).
