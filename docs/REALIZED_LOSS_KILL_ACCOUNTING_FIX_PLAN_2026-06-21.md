# Realized-Loss-Kill Accounting Fix — Plan (2026-06-21)

> **Status: BUILT + GATED (Change-1 only) — awaiting operator deploy.** Scope locked 2026-06-21:
> **Change-1 (merge cost basis)** implemented behind `RF_KILL_ACCT_MERGE_COST_ENABLED` (default OFF =
> byte-identical); **Change-2 (dump-notional typo) + Change-3 (write-downs) DEFERRED** to separate
> single-axis changes. Enablement = **next single-axis step, recorded deviation** (operator-chosen).
> **Gate PASS (laptop, hermetic): `run_audit_v5 --seeds 1 42 1337` INV3/5/7 all PASS; fast pytest 1195
> passed / 0 failed (6 new tests incl. the real-`PositionStore` ordering test); the 1 collection error is
> a pre-existing laptop-only artifact, absent on the box.** Re-run hermetically on the box before enabling.
> Touches the LIVE farmer's loss-accounting path, so the bar is: single-axis, flag-gated (default OFF =
> byte-identical), reversible, gated, operator-deployed. No AI branding. Ground truth: every claim is
> code-verified or live-DB-verified; uncertainties flagged. Extends `realized_loss_kill_bypass` (memory).

## 0. Objective
The realized-loss kill (10%/24h) — the bot's **primary money safety** — reads `SUM(unwinds.pnl WHERE
pnl<0)` as its **sole input** (verified: `reward_farmer.py:1454`, `:1476`; `simple_oversight.py:221`).
Several real-loss paths record `pnl≥0` or no row, so a real loss can **silently bypass the kill**. This
plan fixes the two clean, low-risk paths now (Change-1, Change-2) and sequences the harder no-row paths
next (Change-3). It does **not** change selection, sizing, or the kill *thresholds* — only makes the
kill's existing input *accurate*.

## 1. The defect (verified)
**Change-1 — Merge-at-a-loss booked as PROFIT.** `try_merge` success path
(`dump_manager.py:316-320`) calls `db.log_unwind(side="merge", usd_value=amount)` with **no `vwap_cost`**;
`log_unwind` defaults `vwap_cost=0.0` (`database.py:810`) → `pnl = usd_value − vwap_cost = +amount`
(`database.py:837`). Every merge is booked as pure profit regardless of what the pair cost.
- **Live magnitude (read-only, box, 2026-06-21):** 3 lifetime merge rows, **all positive** (+$107 total;
  +$100 in the last 7d on one ~100-pair merge). Merges are **rare** (≈2/wk) but **large per event** and
  are the bot's both-sides exit on hedged (often adversely-filled) pairs — exactly where a real loss
  would hide. A 100-pair merge of a pair bought at ~$1.10 hides ≈ −$10; a volatile-market pair at ~$1.20
  hides ≈ −$20+. **[magnitude per-event verified-possible; the historical real loss is unrecoverable
  from the merge row — it stores no leg cost — so the exact hidden total is UNKNOWN offline.]**

**Change-2 — Dump live-notional always $0 (`"price"` vs `"fill_price"` key typo).**
`_guardrail_live_notional_per_market` reads `dump_state.get("price")` (`reward_farmer.py:1393`), but the
key constructed in `dump_state` is **`"fill_price"`** (`dump_manager.py:386`; never `"price"`). So
`dp=0.0` always → resting dump SELLs contribute **$0** to live notional → the notional-block, cluster-cap,
and rapid-growth kills are **blind to dump exposure.** (The realized-loss kill is unaffected by this one;
it's the notional-family kills.) Current live dump notional is tiny, so the present-regime impact is small,
but the guardrail is structurally blind. **[verified]**

**Change-3 (sequenced follow-up, NOT built now) — Write-downs with no unwind row.**
`_reconcile_positions`/`set_shares` down (`reward_farmer.py:443-446`) and STALE-CLEANUP `remove_market`
(`reward_farmer.py:831-840`) reduce a tracked position with **no `log_unwind`** → a vanished-share /
held-to-resolution loss produces **no `pnl<0` row at all.** Harder (the loss magnitude at write-down time
is itself uncertain — we don't know the disposal price). Designed in §4, built as its own single-axis step.

## 2. Root cause (one sentence)
The kill's input is `unwinds.pnl<0`; three real-loss paths either compute `pnl` with a **missing/zero cost
basis** (Change-1), feed a **different** guardrail through a **dead dict key** (Change-2), or **never write
an unwind row** (Change-3) — so the loss never appears as the negative number the kill sums.

## 3. The fix (Change-1 + Change-2 — build now)

**Design principle:** each change is **flag-gated, default OFF = byte-identical**, and gets its **own**
flag so the operator can enable + soak **one behavioral axis at a time** (§5). Pure additions; the
legacy code path is preserved verbatim under the `else`.

### Change-1 — merge cost basis  (`config: RF_KILL_ACCT_MERGE_COST_ENABLED`, default False)
At `dump_manager.try_merge` success path. Cost basis IS available (`get_avg_price`, `state.py:522`) and
the normal dump path already uses this exact pattern (`dump_manager.py:137-139`). **Capture avg BEFORE
`record_unwind` (it zeroes `avg_price` at 0 shares — `state.py:126`).**
```python
log.info(f"MERGE {amount:.0f} pairs | {ms.question[:30]}")
_acct = bool(cfg("RF_KILL_ACCT_MERGE_COST_ENABLED"))
if _acct:                                        # capture BEFORE record_unwind resets avg
    _yes_avg = self.positions.get_avg_price(ms.cid, "yes")
    _no_avg  = self.positions.get_avg_price(ms.cid, "no")
self.positions.record_unwind(ms.cid, "yes", amount)
self.positions.record_unwind(ms.cid, "no", amount)
if _acct:
    from price import to_clob
    if 0 < _yes_avg <= 1 and 0 < _no_avg <= 1:
        _merge_cost = amount * (to_clob(_yes_avg, "yes") + to_clob(_no_avg, "no"))
    else:                                        # unknown/corrupt basis: never book a merge as profit
        _merge_cost = amount                     # → pnl = usd_value − amount = 0 (no phantom profit)
        log.warning(f"[UNWIND_COST] cid={ms.cid[:12]} side=merge cost_basis_unknown "
                    f"yes_avg={_yes_avg} no_avg={_no_avg} — floored vwap_cost so pnl<=0 (FX-066 Tier-2 territory)")
else:
    _merge_cost = 0.0                            # legacy: pnl = +amount (byte-identical)
_mg_ok = self.db.log_unwind(
    condition_id=ms.cid, question=ms.question, side="merge",
    shares=amount, sell_price=1.0, usd_value=amount, vwap_cost=_merge_cost,
)
```
- **Economics:** a complete set (1 YES + 1 NO) redeems to **$1** (no taker fee on CTF merge), so
  `usd_value=amount` is correct; cost = `amount × (yes_clob + no_clob)`; `pnl = amount × (1 − yes_clob −
  no_clob)` — **negative iff the pair cost > $1** (an adverse merge), exactly what the kill must see.
- **Flag OFF → `_merge_cost=0.0` → `pnl=+amount` → byte-identical to today.**

### Change-2 — dump notional key  (`config: RF_KILL_ACCT_DUMP_NOTIONAL_ENABLED`, default False)
At `_guardrail_live_notional_per_market` (`reward_farmer.py:1393`):
```python
_key = "fill_price" if cfg("RF_KILL_ACCT_DUMP_NOTIONAL_ENABLED") else "price"
dp = float(dump_state.get(_key) or 0.0)
ds = float(dump_state.get("shares") or 0.0)
notional += dp * ds
```
- **Flag OFF → key `"price"` → `dp=0.0` → byte-identical (the current bug preserved verbatim).**
- Flag ON → key `"fill_price"` (CLOB cost basis) → resting dump SELLs count toward live notional. Orphan/
  startup dumps with unknown basis have `fill_price=0` → contribute 0 (same as today; acceptable).

### Config (config.py) — two knobs, default False, `cfg()` None→off
`RF_KILL_ACCT_MERGE_COST_ENABLED: bool = False` · `RF_KILL_ACCT_DUMP_NOTIONAL_ENABLED: bool = False`

## 4. Change-3 design sketch (sequenced follow-up — NOT this change)
On any write-down that reduces tracked shares without a market exit (`set_shares` down in
`_reconcile_positions`; `remove_market` in STALE-CLEANUP), write a synthetic `log_unwind` row reflecting
the realized loss **before** mutating shares. Open question (why it's separate + harder): the **disposal
value is unknown** at write-down time (shares vanished/reconciled, not sold) — options are (a) value at
last mid, (b) value at cost basis → pnl 0, (c) value at 0 → full-cost loss. Each has failure modes (RT-W*).
Own flag `RF_KILL_ACCT_WRITEDOWN_ROW_ENABLED`, own gate, own soak. Deferred per the operator's sequencing.

## 5. Single-axis honesty + the master-invariant tension (READ THIS)
Both Change-1 and Change-2 are **guardrail behavioral changes** (they alter a kill's *input* → when it
fires). Therefore:
- **Shipping the code flag-OFF is byte-identical → 0 behavioral axes → parallel-safe** (lands during the
  orphan-fix soak exactly as A3 did). Building now does **not** violate P3.
- **ENABLING a flag is a behavioral change → it occupies the single behavioral slot.** The orphan fix
  (`72beaf4`) is currently **mid-soak** (enabled 06-21 ~08:22). Under the master invariant ("≤1 behavioral
  change in its proving window"), the strict path is to **enable these AFTER the orphan soak**, one flag at
  a time (merge-cost, observe, then dump-notional).
- **Nuance (the operator's call):** these are *orthogonal safety-accuracy* fixes (they make kills *see*
  real losses) — they are NOT net-performance levers competing on the A/B promotion metric, so the
  attribution risk that the master invariant guards against is low. Given the kill is **currently blind to
  merge losses while the bot sits ~$20 from the floor**, there is a real safety argument to enable
  merge-cost **soon** as a recorded deviation rather than wait 7 days. **Recommendation: build both now
  (flag-off); enable `RF_KILL_ACCT_MERGE_COST_ENABLED` as the next single-axis step with a recorded
  `ground_rules.md` note; enable `RF_KILL_ACCT_DUMP_NOTIONAL_ENABLED` after.** Operator decides the timing
  vs the orphan soak.

## 6. Critical-evaluator pass (red-team) + hardening
| # | Risk | Mechanism | Hardening |
|---|---|---|---|
| RT-M1 | `avg_price` already 0 when read | `record_unwind` zeroes avg at 0 shares (`state.py:126`) | **capture `get_avg_price` BEFORE the two `record_unwind` calls** (in the code above) |
| RT-M2 | `to_clob` raises `ValueError` on avg ∉ [0,1] (`price.py:31`) → crashes the merge write | corrupt/legacy avg | guard `0 < avg <= 1` before `to_clob`; else conservative floor (no crash) |
| RT-M3 | unknown basis (orphan/startup hedge, avg=0) → can't compute true pnl | `set_shares` registered avg=0 | floor `vwap_cost=amount` → `pnl=0` (never a phantom profit); true magnitude = FX-066 Tier-2 (deferred), stated not hidden |
| RT-M4 | **double-count** a now-negative merge loss (restart re-merges) | re-entry into `try_merge` | **on-chain merge is self-idempotent** — `try_merge_positions` fails the 2nd time (shares already merged) → no 2nd `log_unwind`. No event_id added (a naive time-bucket key could DROP a real loss — worse). Documented; revisit only if a double is observed |
| RT-M5 | enabling retroactively trips the kill | historical merge rows | fix is **forward-only** (historical rows keep `pnl=+amount`, not rewritten); kill sums last 24h → no retroactive trip. [verified] |
| RT-M6 | kill fires more often (false halts) once merge losses count | merges at small spread-loss | merges are rare (3 lifetime); a merge at a *genuine* loss SHOULD count; flag-gate + soak validates; near floor this is desired sensitivity, not a false halt |
| RT-M7 | `amount` > actual held → cost over-stated | clamp mismatch | on a *successful* on-chain merge `amount` pairs existed (balance-verified at `:295`); per-share avg × amount is correct; pre-existing invariant, noted |
| RT-M8 | DRY mode interaction | dry path writes no row | fix is in the **live** branch only (dry returns at `dump_manager.py:266-270`); unaffected |
| RT-N1 | `fill_price=0` for orphan dumps → still 0 | unknown basis | contributes 0 (same as today); acceptable, no regression |
| RT-N2 | notional-family kills fire more | dump notional now counted | tiny in current regime; flag-gate + soak; single-axis enable |
| RT-N3 | `fill_price` (cost basis) ≠ current sell limit — wrong exposure measure? | proxy choice | cost-basis×shares ≈ value we still owe = the intended "live exposure" (`:1389`); matches original intent; acceptable proxy |
| RT-X1 | gate run in-place on the box → ~24 config-pollution failures mistaken for regressions | non-hermetic tests | **run the gate HERMETICALLY** (clean `/tmp` worktree, no `config_overrides.json`) — see `repo_config_defaults_not_live` memory |
| RT-X2 | farmer restart to deploy clears the RAM-only kill flag | `farmer_kill_flag_ram_only` | **kill-state check BEFORE restart** (cardinal rule); operator deploys |
| RT-X3 | two flags enabled together = 2 axes | accidental multi-axis | separate flags; enable one at a time; §5 sequencing |
| RT-X4 | byte-identical claim wrong | regression when "off" | **byte-identical test** (both flags off → merge pnl=+amount, dump notional=0) is the proof obligation |

**Completeness honesty:** two-pass effort, not a proof of exhaustiveness. Re-run this pass before code lands.

## 7. Tests (the gate)
- `test_merge_cost_basis_on`: pair bought >$1 → flag ON → merge `pnl<0` (kill-visible); pair <$1 → `pnl>0`.
- `test_merge_unknown_basis_floor`: avg=0 (and avg out-of-range) → flag ON → `vwap_cost=amount` → `pnl=0`, **no crash**.
- `test_merge_byte_identical_off`: flag OFF → merge `pnl=+amount` (exactly today).
- `test_dump_notional_typo_on`: `dump_state{fill_price,shares}` → flag ON → notional includes `fill_price×shares`.
- `test_dump_notional_byte_identical_off`: flag OFF → dump contributes 0.
- **Full gate (live-farmer change), run HERMETICALLY:** `run_audit_v5 --seeds 1 42 1337` + `pytest tests/ --ignore=tests/test_simulation.py -q` in a clean checkout with no `config_overrides.json`.

## 8. Rollout (operator-gated; I implement + gate, operator deploys)
1. Implement Change-1 + Change-2, both flags default OFF → **byte-identical**; add tests; gate hermetically.
2. Operator `git pull` + **kill-state check** + restart `polymarket-farmer` (FARMER change) — behavior unchanged (flags off).
3. Operator enables `RF_KILL_ACCT_MERGE_COST_ENABLED=true` (hot-reload), records in `ground_rules.md`, soaks; then `RF_KILL_ACCT_DUMP_NOTIONAL_ENABLED`. One axis at a time (§5).
- **Revert:** flag(s) off (hot-reload) → instant byte-identical; code inert when off.

## 9. Decisions to lock (before I write code)
1. **Build scope now:** Change-1 (merge cost) + Change-2 (dump-notional typo) — confirm, or merge-only first.
2. **Flag strategy:** two separate flags, default off (single-axis enable). Confirm (recommended).
3. **Enablement sequencing** (§5): enable merge-cost as the next single-axis step (recorded deviation) vs wait for the orphan ≥7-day soak. Operator's governance call.
4. **Unknown-basis merge floor:** `pnl=0` (no phantom profit), true magnitude deferred to FX-066 Tier-2. Confirm acceptable.
