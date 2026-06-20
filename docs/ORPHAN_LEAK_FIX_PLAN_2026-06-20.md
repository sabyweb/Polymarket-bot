# Orphan-Position Leak — Fix Plan (2026-06-20)

> **Status: DESIGN — for review before any code.** Touches the LIVE farmer's reconciliation path, so:
> single-axis, flag-gated (default OFF = byte-identical), reversible, gated (run_audit_v5 + fast pytest),
> operator-deployed. No AI branding. Ground truth: every claim below is code-verified or live-journal-verified;
> uncertainties are flagged.

## 1. The incident (verified)
On `0xba8526e4bf87` ("Will Adriano Espaillat be the Democratic Nominee for NY-13"), the midpoint sat flat at
0.485 all morning, then swept 0.485→0.605 in ~2 min (12:08–12:10 UTC). The bot **recorded only 100 shares**
(50 NO @0.51, 50 YES @0.57, then a 50-pair merge), but on-chain it held **220 NO**. The ~170 untracked NO
shares sat invisible for ~26 min until the 30-min position sync found + dumped them at **−$38.03**.

## 2. Root cause (code-verified + live-journal-verified)
The proximate cause is an **under-reported fill that is disqualified from per-cycle reconciliation**:

1. The 12:08 NO order filled; `get_order` reported `matched=50`; `handle_fill` recorded 50 and marked the
   side **`primary_handled`** (`order_lifecycle.py:515,545`). On-chain, far more NO filled during the sweep.
2. The per-cycle drift catch-up `_reconcile_balance_drift` runs **only for `cids_processed − primary_handled`**
   (`order_lifecycle.py:579`) — i.e. sides whose order vanished *without* a `handle_fill`. A side that recorded
   a partial fill is **excluded**, so the under-reported remainder is **never drift-checked**.
   **Live proof: `RECONCILE_DRIFT` count = 0 in the 3h window — the catch-up never fired.**
3. The only full on-chain-vs-tracked reconciliation is `_sync_exchange_positions`, **hardcoded to 1800s**
   (`reward_farmer.py:2735`), and it is **cid-level only** (`orphan_cids = exchange_cids − tracked_cids`,
   `:728`) — it has **no share-level check** for a still-tracked cid. So orphan exposure is unbounded for up
   to ~30 min, and only caught at all here because the cid had dropped out of `self.markets`.
4. **The entire fill-rate safety stack is blind to this**: the fill-rate breaker (`order_lifecycle.py:1205`),
   cross-market storm (`reward_farmer.py:2364`) and per-market fill-kill are all fed *only* by recorded fills
   (`handle_fill` appends to `fill_times`/`kill_fill_times`, `:766,:771`). **Live proof: 0 breakers/storms
   fired all day.** A leak that bypasses recording bypasses every throttle/kill meant to stop it.
5. **Cost-basis corruption**: when the sync recovered the orphan it set the basis from `fills_vwap` (recorded
   fills only → 0.51), even though the data-api `/positions` response it already fetched carries the **true
   on-chain `avgPrice`** (`reward_farmer.py:721`), which it ignores.

**Five confirmed gaps:** (i) drift dedup is a 5-min time bucket that suppresses intra-window catch-ups
(`order_lifecycle.py:38,350` + FX-065 skip `:648`) — a real defect, though not the proximate cause here;
(ii) drift check excludes `primary_handled` sides — **the proximate cause**; (iii) single-order-per-side +
once-per-30s detection can't keep up with a sweep; (iv) 30-min sync is the only full reconciliation and is
cid-level; (v) all fill-rate safeties are fed only by recorded fills. **Uncertainty:** the exact split of
how the 170 shares arrived (one under-reported fill vs several undetected re-fills) is not determinable from
code/logs alone — but the *fix does not depend on it* (it reconciles on-chain truth regardless).

## 3. The fix — Direction B: fast, share-level orphan reconciliation (RECOMMENDED)
A **backstop that bounds the harm robustly**, in ONE place, touching the delicate fill-detection hot path as
little as possible. Extend `_sync_exchange_positions` (which already fetches per-cid-per-side on-chain shares
**and** avgPrice in one `/positions` call) to also reconcile **share-level** drift, and make its cadence a knob.

- **Share-level reconciliation (new, flag-gated `RF_SYNC_SHARE_DRIFT_ENABLED`, default False):** for every
  `(cid, side)` in the `/positions` response, if `on_chain_shares > tracked_shares + RF_ORPHAN_DRIFT_MIN_SHARES`
  (default 1.0), treat the excess as an untracked orphan: `set_shares` to the on-chain count **using the
  on-chain `avgPrice`** for cost basis (fixes gap #5), then dump it via the existing `dump_position` path.
  When the flag is OFF, the sync behaves byte-identically to today (cid-level only).
- **Cadence knob `RF_EXCHANGE_SYNC_SECS` (default 1800 = unchanged):** replace the hardcoded `1800` at
  `reward_farmer.py:2735` with `cfg("RF_EXCHANGE_SYNC_SECS")`. Lowering it (e.g. 120) bounds orphan exposure
  from ~30 min to ~2 min. One `/positions` call per run → negligible API cost.
- **Deploy = enable the flag + lower the cadence.** Both default to no-op, so shipping the code changes nothing.

**Why B over Direction A (fix the drift path: drift-check `primary_handled` sides + content-based dedup):**
A is the deeper root fix and catches within one ~30s cycle, but it (1) edits the *hot* fill-detection path
(higher regression risk on the most delicate code), (2) adds a `balanceOf` per fill, (3) is two coupled
changes (gaps i+ii), and (4) doesn't fix cost basis. B is one coherent backstop, robust to *all five gaps*
(it reconciles on-chain truth regardless of why detection missed), near-zero API cost, and fixes cost basis.
**Recommendation: ship B as the bounding backstop now; consider A as a follow-up if B's ~2-min window is
still too much exposure.**

## 4. Critical-evaluator pass (red-team) + hardening
| # | Risk | Mechanism | Hardening |
|---|---|---|---|
| RT-1 | **Transient-lag false reconcile** — a fill just happened, on-chain reflects it, position store not updated yet → sync sees drift and dumps | at faster cadence this race is more likely | (a) require the drift to **persist across 2 consecutive syncs** before acting (debounce); (b) the fill was going to be dump-on-fill anyway, so dumping it is *aligned* — worst case is dumping a few seconds early; (c) generous `RF_ORPHAN_DRIFT_MIN_SHARES` |
| RT-2 | **Double-dump** — primary path is dumping the tracked 50 while the sync dumps the on-chain 220 | overlapping dump triggers | reconcile with `set_shares` (absolute, idempotent); verify `dump_position` no-ops/clamps when a dump SELL is already resting for that side (it balance-clamps to on-exchange shares, `dump_manager.py:362`); the debounce (RT-1) also reduces overlap |
| RT-3 | **Dumping into a falling/illiquid book realizes a loss / fails** | the orphan is already adverse | this *bounds* the loss (catch at 2 min not 30 min); existing dump decay + unliquidatable + FX-071 slippage floor apply; faster catch ≈ smaller loss, never larger |
| RT-4 | **Faster catch doesn't PREVENT the loss** — the adverse fill already happened | B is detection, not prevention | explicitly out of scope for B; gap (v) prevention (placement throttle keyed to on-chain share velocity) is the **follow-up axis** (§6) |
| RT-5 | **Cost-basis from on-chain avgPrice is wrong/zero** for a true legacy orphan | `/positions avgPrice` may be 0 for some | fall back to the existing `fills_vwap`/Tier-1 floor when avgPrice ≤ 0 (current behavior) — strictly better than today |
| RT-6 | **API cost / rate-limit** at low cadence | one `/positions` call per run | 120s ≈ 1 call/2min, negligible vs the farmer's existing call volume; `/positions` is one call for ALL markets |
| RT-7 | **Re-registers a just-dumped orphan repeatedly** (on-chain CTF balance lingers) | FX-007 unliquidatable + CTF-never-clears | reuse the existing `is_unliquidatable` skip (`reward_farmer.py:738`) and the stale-clean path; share-level path must honor the same guard |
| RT-8 | **Single-axis honesty** — the fix has two knobs (enable + cadence) | enabled together | it is ONE behavior (share-level reconciliation) with two facets; both default to no-op; gated + soak-proven as one change. Stated plainly, not hidden. |

## 5. Tests (the gate)
- Unit: share-level drift detection (on_chain > tracked+thresh → reconcile+dump; ≤thresh → no-op); on-chain
  avgPrice used for basis (fallback to vwap when 0); flag OFF → byte-identical (no share-level action);
  `is_unliquidatable` honored; debounce (2-sync persistence) before acting.
- `RF_EXCHANGE_SYNC_SECS` cadence knob respected (default 1800).
- **Full gate** (live-farmer code change): `run_audit_v5 --seeds 1 42 1337` + fast pytest.

## 6. Follow-ups (separate axes, NOT this change)
- **Prevention (gap v):** a real-time placement throttle that stops placing on a side when on-chain share
  velocity (not recorded-fill velocity) spikes — closes the "keeps placing into a sweep" hole and makes the
  safety stack see leaks. Behavioral; own gate.
- **Detection depth (Direction A):** drift-check `primary_handled` sides + content-based dedup, to catch
  under-reports within one cycle. Hot-path; own gate.
- **Safety-stack blindness (gap v):** feed the fill-rate breaker/kill from an on-chain-share signal so a
  leak can trip a halt. Own gate.

## 7. Rollout (operator-gated)
1. Implement B, flag default OFF + cadence default 1800 → **byte-identical**; gate (run_audit_v5 + pytest).
2. Operator `git pull` + restart `polymarket-farmer` (this is a FARMER change) — behavior unchanged.
3. Operator enables `RF_SYNC_SHARE_DRIFT_ENABLED=true` + sets `RF_EXCHANGE_SYNC_SECS=120` (hot-reload).
- **Revert:** flag off + cadence → 1800 (hot-reload). Fully reversible.
- **Note:** this is a **farmer** restart (unlike A3's oversight restart) — re-verify no active kill first;
  the farmer's RAM-only kill flag (memory `farmer_kill_flag_ram_only`) clears on its restart, so confirm
  state before restarting.
