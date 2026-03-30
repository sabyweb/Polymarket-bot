#!/usr/bin/env python3
"""Paper Trading Simulator v2 — clean rewrite.

Runs multiple strategies in parallel against LIVE order book data,
simulating fills, tracking rewards, and producing a comparison table.

Usage:
    python paper_trader_v2.py --duration 1h --balance 3300
    python paper_trader_v2.py --duration 10m --balance 500
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("paper_v2")


# ═══════════════════════════════════════════════════════════════════════
# 1. STRATEGY DEFINITIONS
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class Strategy:
    name: str
    shares_per_side: float        # fixed share count per order side
    max_markets: int              # how many markets to trade
    min_daily_rate: float         # minimum $/day to consider a market
    min_max_spread: float         # skip markets with reward window < this
    placement_ticks_inside: int   # how many ticks inside the reward edge to place
    volatility_filter: bool       # if True, skip volatile keyword markets
    dump_on_fill: bool            # if True, dump at market on fill; else hold
    max_liquidity: float          # skip markets with more liquidity than this (0 = no limit)


STRATEGIES = [
    # User profile: mimics manual strategy — min_size on zero-competition markets
    Strategy(
        name="user_profile",
        shares_per_side=50,       # min_size on most markets
        max_markets=100,          # cover as many as possible
        min_daily_rate=1,         # even $1/day markets with zero competition
        min_max_spread=0.01,
        placement_ticks_inside=1, # 1 tick inside edge (safe, but with no competition = 100% Q)
        volatility_filter=False,
        dump_on_fill=True,
        max_liquidity=500,        # ONLY markets with < $500 liquidity (zero competition)
    ),
    # Broader: same as user but higher liq threshold
    Strategy(
        name="low_comp_50sh",
        shares_per_side=50,
        max_markets=100,
        min_daily_rate=1,
        min_max_spread=0.01,
        placement_ticks_inside=1,
        volatility_filter=False,
        dump_on_fill=True,
        max_liquidity=5000,       # markets with < $5K liquidity
    ),
    # Medium: 200 shares, 2 ticks inside, moderate competition
    Strategy(
        name="mid_comp_200sh",
        shares_per_side=200,
        max_markets=50,
        min_daily_rate=5,
        min_max_spread=0.01,
        placement_ticks_inside=2,
        volatility_filter=False,
        dump_on_fill=True,
        max_liquidity=50000,
    ),
    # Aggressive: 500 shares, 3 ticks inside
    Strategy(
        name="aggressive_500sh",
        shares_per_side=500,
        max_markets=30,
        min_daily_rate=10,
        min_max_spread=0.01,
        placement_ticks_inside=3,
        volatility_filter=False,
        dump_on_fill=True,
        max_liquidity=0,          # no limit
    ),
]


# ═══════════════════════════════════════════════════════════════════════
# 2. SESSION — one per strategy, fully isolated
# ═══════════════════════════════════════════════════════════════════════

VOLATILE_KEYWORDS = [
    "iran", "crude oil", "invade", "forces enter", "regime fall",
    "ceasefire", "military", "war ", "attack", "strike", "missile",
    "nuclear", "sanctions",
]


def get_merged_book(book_provider, yes_tid: str, no_tid: str) -> dict | None:
    """Fetch YES + NO order books and merge into a single YES-equivalent view.

    Mirrors order_manager.get_order_book() logic:
    - NO asks → derived YES bids (1 - ask_price)
    - NO bids → derived YES asks (1 - bid_price)
    Returns dict with "bids" and "asks" keys, each list of {"price": float, "size": float}.
    """
    try:
        ob_yes = book_provider.get_order_book(yes_tid)
        if not ob_yes:
            return None

        all_bids = []  # (price, size) tuples
        all_asks = []

        for b in getattr(ob_yes, "bids", []):
            all_bids.append((float(b.price), float(b.size)))
        for a in getattr(ob_yes, "asks", []):
            all_asks.append((float(a.price), float(a.size)))

        # Merge NO book
        ob_no = book_provider.get_order_book(no_tid)
        if ob_no:
            for a in getattr(ob_no, "asks", []):
                derived = round(1.0 - float(a.price), 4)
                if derived > 0:
                    all_bids.append((derived, float(a.size)))
            for b in getattr(ob_no, "bids", []):
                derived = round(1.0 - float(b.price), 4)
                if derived < 1:
                    all_asks.append((derived, float(b.size)))

        all_bids.sort(key=lambda x: x[0], reverse=True)
        all_asks.sort(key=lambda x: x[0])

        if not all_bids or not all_asks:
            return None

        return {
            "bids": [{"price": p, "size": s} for p, s in all_bids],
            "asks": [{"price": p, "size": s} for p, s in all_asks],
        }
    except Exception:
        return None


def is_volatile(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in VOLATILE_KEYWORDS)


@dataclass
class OrderSlot:
    """Tracks one live order per side per market."""
    order_id: str | None = None
    price: float = 0.0
    shares: float = 0.0


@dataclass
class Inventory:
    """Tracks accumulated inventory from fills per market."""
    yes_shares: float = 0.0
    no_shares: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0


class Session:
    """Fully isolated paper trading session for one strategy."""

    def __init__(self, strategy: Strategy, balance: float, real_client, book_cache):
        self.strategy = strategy
        self.name = strategy.name

        # Isolated DB
        self.db_path = f"paper_{strategy.name}.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

        import database
        self.db = database.BotDatabase(db_path=self.db_path)

        # Isolated paper client
        from paper_client import PaperClient
        self.pc = PaperClient(
            real_client=real_client,
            initial_balance=balance,
            fill_model="cross_through",
            queue_position_factor=0.5,
            label=strategy.name,
            book_cache=book_cache,
        )

        # Isolated reward tracker (initialized against our DB)
        self._init_reward_tracker()

        # Per-market state
        self.orders: dict[str, dict[str, OrderSlot]] = {}  # cid → {"yes": OrderSlot, "no": OrderSlot}
        self.inventory: dict[str, Inventory] = {}           # cid → Inventory

        # Counters
        self.cycle_count = 0
        self.start_time = time.time()
        self.last_reward_log = 0.0  # force first hourly to run after 1 cycle

    def _init_reward_tracker(self):
        """Create a RewardTracker that reads/writes to OUR db."""
        import database as db_mod
        from reward_tracker import RewardTracker
        orig = db_mod._instance
        db_mod._instance = self.db
        try:
            self.reward_tracker = RewardTracker()
        finally:
            db_mod._instance = orig

    def with_db(self, fn):
        """Execute fn() with database._instance pointed at our DB."""
        import database as db_mod
        orig = db_mod._instance
        db_mod._instance = self.db
        try:
            return fn()
        finally:
            db_mod._instance = orig


# ═══════════════════════════════════════════════════════════════════════
# 3. SINGLE CYCLE — the core logic, clean and linear
# ═══════════════════════════════════════════════════════════════════════

def run_cycle(session: Session, markets: list[dict]):
    """Run one cycle for a session. Steps:
    1. Filter markets for this strategy
    2. Simulate fills on existing orders
    3. Detect fills → merge or dump
    4. Place new orders where needed
    5. Record reward tracking data
    """
    s = session
    pc = s.pc
    strat = s.strategy
    s.cycle_count += 1

    # ── Step 1: Filter markets ──────────────────────────────────────
    eligible = []
    for m in markets:
        rate = m.get("daily_rate", 0)
        if rate < strat.min_daily_rate:
            continue
        ms = m.get("max_spread", 0.03)
        if ms < strat.min_max_spread:
            continue
        if strat.volatility_filter and is_volatile(m.get("question", "")):
            continue
        if strat.max_liquidity > 0 and m.get("liquidity", 0) > strat.max_liquidity:
            continue
        tokens = m.get("token_ids", [])
        if len(tokens) < 2:
            continue
        eligible.append(m)

    if not eligible:
        return

    # Register markets with paper client
    for m in eligible:
        tids = m["token_ids"]
        pc.register_market(m["condition_id"], tids[0], tids[1])

    # ── Step 2: Simulate fills ──────────────────────────────────────
    pc.simulate_fills()

    # ── Step 3: Detect fills, merge, dump ───────────────────────────
    for m in eligible:
        cid = m["condition_id"]
        tids = m["token_ids"]
        yes_tid, no_tid = tids[0], tids[1]

        slots = s.orders.get(cid, {})
        inv = s.inventory.setdefault(cid, Inventory())

        for side, tid in [("yes", yes_tid), ("no", no_tid)]:
            slot = slots.get(side)
            if not slot or not slot.order_id:
                continue

            status = pc._paper_get_order(slot.order_id)
            if status.get("status") != "MATCHED":
                continue

            matched = float(status.get("size_matched", 0))
            fill_price = float(status.get("price", 0))
            if matched < 1.0:
                slot.order_id = None
                continue

            # Record fill
            setattr(inv, f"{side}_shares", getattr(inv, f"{side}_shares") + matched)
            setattr(inv, f"{side}_cost", getattr(inv, f"{side}_cost") + matched * fill_price)

            log.info(
                f"[{s.name}] FILL {side.upper()} {matched:.0f}sh @ {fill_price:.4f} | "
                f"Y={inv.yes_shares:.0f} N={inv.no_shares:.0f} | {m['question'][:30]}"
            )

            def _log_fill(cid=cid, m=m, side=side, matched=matched, fill_price=fill_price):
                from database import get_db
                get_db().log_fill(
                    condition_id=cid, question=m.get("question", ""),
                    side=side, fill_type="FULL",
                    shares=matched, price=fill_price,
                    clob_cost=fill_price, usd_value=matched * fill_price,
                )
            s.with_db(_log_fill)
            slot.order_id = None

        # ── Merge ───────────────────────────────────────────────────
        merge_qty = min(inv.yes_shares, inv.no_shares)
        if merge_qty >= 1.0:
            result = pc.merge_positions(cid, merge_qty)
            if isinstance(result, dict) and result.get("success"):
                yc = (inv.yes_cost / inv.yes_shares * merge_qty) if inv.yes_shares > 0 else 0
                nc = (inv.no_cost / inv.no_shares * merge_qty) if inv.no_shares > 0 else 0
                pnl = merge_qty - yc - nc  # $1 per merged pair
                log.info(f"[{s.name}] MERGE {merge_qty:.0f} pairs pnl=${pnl:+.2f} | {m['question'][:30]}")
                inv.yes_shares -= merge_qty
                inv.no_shares -= merge_qty
                inv.yes_cost -= yc
                inv.no_cost -= nc

                def _log_merge(cid=cid, m=m, merge_qty=merge_qty, yc=yc, nc=nc, pnl=pnl):
                    from database import get_db
                    get_db().log_unwind(
                        condition_id=cid, question=m.get("question", ""),
                        side="merge", shares=merge_qty,
                        sell_price=1.0, usd_value=merge_qty,
                        vwap_cost=yc + nc, pnl=pnl,
                    )
                s.with_db(_log_merge)

        # ── Dump single-side inventory ──────────────────────────────
        if strat.dump_on_fill:
            for side, tid in [("yes", yes_tid), ("no", no_tid)]:
                remaining = getattr(inv, f"{side}_shares")
                other_side = "no" if side == "yes" else "yes"
                other_remaining = getattr(inv, f"{other_side}_shares")
                if remaining < 1.0:
                    continue
                if other_remaining >= 1.0:
                    continue  # wait for merge

                try:
                    # For YES dump: sell YES tokens → use YES book bids
                    # For NO dump: sell NO tokens → use NO book bids
                    dump_tid = tid
                    raw_book = (pc._book_cache or pc._real_client).get_order_book(dump_tid)
                    if not raw_book or not getattr(raw_book, 'bids', None):
                        log.warning(f"[{s.name}] Dump {side}: no book for {dump_tid[:16]}")
                        continue

                    dump_clob_price = float(raw_book.bids[0].price)

                    # For YES: dump_price = clob_price (we sell YES at YES bid)
                    # For NO:  dump_price = clob_price (we sell NO at NO bid)
                    # Revenue = shares × clob_price
                    available = pc._token_balances.get(tid, 0.0)
                    dump_size = min(remaining, available)
                    if dump_size < 1.0:
                        log.warning(
                            f"[{s.name}] Dump {side}: inv={remaining:.0f} but tokens={available:.0f}, resetting"
                        )
                        setattr(inv, f"{side}_shares", 0.0)
                        setattr(inv, f"{side}_cost", 0.0)
                        continue

                    dump_revenue = dump_size * dump_clob_price

                    # Instant dump: direct balance mutation
                    with pc._lock:
                        pc._token_balances[tid] = pc._token_balances.get(tid, 0.0) - dump_size
                        pc._usdc_balance += dump_revenue

                    cost_frac = dump_size / remaining if remaining > 0 else 1.0
                    dump_cost = getattr(inv, f"{side}_cost") * cost_frac
                    dump_pnl = dump_revenue - dump_cost

                    log.info(
                        f"[{s.name}] DUMP {side.upper()} {dump_size:.0f}sh @ {dump_clob_price:.4f} "
                        f"rev=${dump_revenue:.2f} cost=${dump_cost:.2f} pnl=${dump_pnl:+.2f} | "
                        f"{m['question'][:30]}"
                    )

                    def _log_dump(cid=cid, m=m, side=side, dump_size=dump_size,
                                  dump_clob_price=dump_clob_price, dump_revenue=dump_revenue,
                                  dump_cost=dump_cost, dump_pnl=dump_pnl):
                        from database import get_db
                        get_db().log_unwind(
                            condition_id=cid, question=m.get("question", ""),
                            side=side, shares=dump_size,
                            sell_price=dump_clob_price, usd_value=dump_revenue,
                            vwap_cost=dump_cost, pnl=dump_pnl,
                        )
                    s.with_db(_log_dump)

                    setattr(inv, f"{side}_shares", getattr(inv, f"{side}_shares") - dump_size)
                    setattr(inv, f"{side}_cost", getattr(inv, f"{side}_cost") - dump_cost)
                except Exception as e:
                    log.error(f"[{s.name}] Dump {side} FAILED: {e}", exc_info=True)

    # ── Step 4: Place orders ────────────────────────────────────────
    for m in eligible:
        cid = m["condition_id"]
        tids = m["token_ids"]
        yes_tid, no_tid = tids[0], tids[1]
        max_spread = m.get("max_spread", 0.03)
        min_size = m.get("min_size", 5)
        tick = m.get("tick_size", 0.01)
        decimals = max(2, len(str(tick).rstrip('0').split('.')[-1]))

        slots = s.orders.setdefault(cid, {"yes": OrderSlot(), "no": OrderSlot()})

        # Get MERGED order book (YES + NO combined) for accurate midpoint
        merged = get_merged_book(pc._book_cache or pc._real_client, yes_tid, no_tid)
        if not merged or not merged["bids"] or not merged["asks"]:
            continue
        best_bid = float(merged["bids"][0]["price"])
        best_ask = float(merged["asks"][0]["price"])
        midpoint = (best_bid + best_ask) / 2

        if best_ask - best_bid > 0.15:  # skip markets with extremely wide spreads
            continue

        # Compute edge prices — N ticks INSIDE the reward window edge
        # (exactly AT max_spread gives Q-score = 0 due to the >= check)
        ticks_in = strat.placement_ticks_inside
        edge_bid = round(midpoint - max_spread + tick * ticks_in, decimals)
        edge_ask = round(midpoint + max_spread - tick * ticks_in, decimals)

        # Clamp: stay within valid price range, but DON'T clamp to best bid/ask
        # (we WANT to be far from best — that's the whole point of edge placement)
        edge_bid = max(0.01, edge_bid)
        edge_ask = min(0.99, edge_ask)

        # Share count
        yes_shares = max(min_size, strat.shares_per_side)
        no_clob = round(1.0 - edge_ask, decimals)
        no_clob = max(0.01, no_clob)
        no_shares = max(min_size, strat.shares_per_side)

        # Balance check
        total_cost = yes_shares * edge_bid + no_shares * no_clob
        if total_cost > pc._usdc_balance * 0.95:
            scale = (pc._usdc_balance * 0.9) / total_cost if total_cost > 0 else 0
            yes_shares = max(min_size, yes_shares * scale)
            no_shares = max(min_size, no_shares * scale)

        from py_clob_client.clob_types import OrderArgs
        from py_clob_client.order_builder.constants import BUY

        # Place YES bid
        if not slots["yes"].order_id:
            try:
                args = OrderArgs(token_id=yes_tid, price=edge_bid, size=float(yes_shares), side=BUY)
                resp = pc.create_and_post_order(args)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                if oid:
                    slots["yes"] = OrderSlot(order_id=oid, price=edge_bid, shares=yes_shares)
                    def _log_yes(cid=cid, edge_bid=edge_bid, yes_shares=yes_shares, oid=oid):
                        from database import get_db
                        get_db().log_order_placed(condition_id=cid, side="yes", price=edge_bid, size=float(yes_shares), order_id=oid)
                    s.with_db(_log_yes)

                    if s.cycle_count <= 2:
                        log.info(f"[{s.name}] BID YES @ {edge_bid:.3f} ({yes_shares:.0f}sh) | best={best_bid:.3f} mid={midpoint:.3f} | {m['question'][:25]}")
            except Exception as e:
                log.debug(f"[{s.name}] YES order failed: {e}")

        # Place NO ask
        if not slots["no"].order_id:
            try:
                args = OrderArgs(token_id=no_tid, price=no_clob, size=float(no_shares), side=BUY)
                resp = pc.create_and_post_order(args)
                oid = resp.get("orderID") if isinstance(resp, dict) else None
                if oid:
                    slots["no"] = OrderSlot(order_id=oid, price=edge_ask, shares=no_shares)
                    def _log_no(cid=cid, edge_ask=edge_ask, no_shares=no_shares, oid=oid):
                        from database import get_db
                        get_db().log_order_placed(condition_id=cid, side="no", price=edge_ask, size=float(no_shares), order_id=oid)
                    s.with_db(_log_no)

                    if s.cycle_count <= 2:
                        log.info(f"[{s.name}] ASK NO @ {edge_ask:.3f} (clob={no_clob:.3f}, {no_shares:.0f}sh) | best={best_ask:.3f} mid={midpoint:.3f} | {m['question'][:25]}")
            except Exception as e:
                log.debug(f"[{s.name}] NO order failed: {e}")

        # ── Step 5: Record reward tracking ──────────────────────────
        has_yes = slots["yes"].order_id is not None
        has_no = slots["no"].order_id is not None

        # merged book is already a dict with "bids"/"asks" keys
        book_dict = merged

        s.reward_tracker.get_or_create(
            condition_id=cid,
            question=m.get("question", ""),
            daily_rate=m.get("daily_rate", 0),
            max_spread=max_spread,
        )
        s.reward_tracker.record_cycle(
            condition_id=cid,
            has_yes_order=has_yes, has_no_order=has_no,
            bid_price=edge_bid if has_yes else 0,
            ask_price=edge_ask if has_no else 0,
            inventory_usd=0.0,
            cooldown_active=False, skew_active=False,
            cycle_duration_secs=30.0,
            midpoint=midpoint,
            bid_size=float(yes_shares) if has_yes else 0,
            ask_size=float(no_shares) if has_no else 0,
            order_book=book_dict,
        )

        # Cycle snapshot every 10 cycles
        if s.cycle_count % 10 == 0:
            def _log_snap(cid=cid, best_bid=best_bid, best_ask=best_ask,
                          edge_bid=edge_bid, edge_ask=edge_ask, has_yes=has_yes, has_no=has_no):
                from database import get_db
                get_db().log_cycle_snapshot(
                    cycle_num=s.cycle_count, condition_id=cid,
                    best_bid=best_bid, best_ask=best_ask,
                    our_bid=edge_bid, our_ask=edge_ask,
                    active_orders=int(has_yes) + int(has_no), unwind_orders=0,
                )
            s.with_db(_log_snap)

    # ── Persist reward tracker (inside DB context) ──────────────────
    def _save_rewards():
        s.reward_tracker._save()
    s.with_db(_save_rewards)

    # ── Trigger hourly reward estimation ────────────────────────────
    now = time.time()
    if now - s.last_reward_log >= 3600:
        s.last_reward_log = now
        def _hourly():
            s.reward_tracker.maybe_log_hourly(eligible)
        s.with_db(_hourly)
        # Force the internal timer so it doesn't skip
        s.reward_tracker._last_hourly_log = 0


# ═══════════════════════════════════════════════════════════════════════
# 4. RESULTS — query each session's DB
# ═══════════════════════════════════════════════════════════════════════

def get_results(session: Session) -> dict:
    """Query a session's DB for final results."""
    import sqlite3
    db = sqlite3.connect(session.db_path)
    db.row_factory = sqlite3.Row

    r = db.execute(
        "SELECT COALESCE(SUM(CASE WHEN side='yes' THEN shares*price ELSE shares*clob_cost END),0),"
        "COUNT(*) FROM fills"
    ).fetchone()
    bought, fills = r[0], r[1]

    r = db.execute("SELECT COALESCE(SUM(usd_value),0), COALESCE(SUM(pnl),0), COUNT(*) FROM unwinds").fetchone()
    sold, pnl, unwinds = r[0], r[1], r[2]

    r = db.execute("SELECT COALESCE(SUM(loss_usd),0), COUNT(*) FROM stop_losses").fetchone()
    stop_loss, stops = r[0], r[1]

    total_pos = 0
    for p in db.execute("SELECT * FROM positions").fetchall():
        total_pos += p['yes_shares'] * p['yes_avg_price'] + p['no_shares'] * p['no_avg_price']

    total_rewards = 0
    reward_details = []
    for r in db.execute("SELECT data FROM reward_market_stats").fetchall():
        d = json.loads(r[0])

        # Compute reward estimate directly from Q-score data
        # (don't rely on maybe_log_hourly which has a 1-hour gate)
        q_share_pct = 0.0
        if d.get('total_market_q', 0) > 0 and d.get('q_score_samples', 0) > 0:
            q_share_pct = d['total_q_score'] / d['total_market_q']

        # How many hours of data?
        on_book_hrs = d.get('time_on_book_secs', 0) / 3600.0
        uptime = 1.0  # assume 100% uptime for paper trading

        # est_reward = daily_rate × q_share × uptime × hours / 24
        daily_rate = d.get('daily_rate', 0)
        est = daily_rate * q_share_pct * uptime * on_book_hrs / 24.0

        total_rewards += est
        if d.get('total_cycles', 0) > 0:
            reward_details.append({
                'q': d['question'][:40], 'rate': daily_rate,
                'est': est, 'q_share': q_share_pct * 100,
                'both': d.get('cycles_both_sides', 0), 'cycles': d.get('total_cycles', 0),
            })

    orders = db.execute("SELECT COUNT(*) FROM orders_placed").fetchone()[0]
    net = sold + total_pos - bought
    db.close()

    return {
        'name': session.name, 'fills': fills, 'unwinds': unwinds,
        'stops': stops, 'orders': orders,
        'bought': bought, 'sold': sold, 'pos': total_pos,
        'stop_loss': stop_loss, 'rewards': total_rewards,
        'net': net, 'net_all': net + total_rewards,
        'details': reward_details,
        'balance': session.pc._usdc_balance,
    }


def print_results(results: list[dict], duration_hrs: float):
    """Print comparison table."""
    print(f"\n{'='*100}")
    print(f"  PAPER TRADING RESULTS ({duration_hrs:.1f} hours, {len(results)} strategies)")
    print(f"{'='*100}")

    header = f"{'Metric':<22s}"
    for r in results:
        header += f" | {r['name']:>12s}"
    print(header)
    print('-' * (24 + 15 * len(results)))

    for label, key, fmt in [
        ("Orders placed", "orders", "d"),
        ("Fills", "fills", "d"),
        ("Unwinds", "unwinds", "d"),
        ("Total bought", "bought", "$"),
        ("Total sold", "sold", "$"),
        ("Open positions", "pos", "$"),
        ("Est rewards", "rewards", "$"),
        ("USDC balance", "balance", "$"),
        ("Trading P&L", "net", "+"),
        ("NET (all-in)", "net_all", "+"),
    ]:
        row = f"{label:<22s}"
        for r in results:
            v = r.get(key, 0)
            if fmt == "d":
                row += f" | {int(v):>12d}"
            elif fmt == "+":
                row += f" | ${v:>+11.2f}"
            else:
                row += f" | ${v:>11.2f}"
        print(row)

    # Per-strategy reward breakdown
    for r in results:
        if r.get('details'):
            print(f"\n--- {r['name']} reward breakdown ---")
            for d in sorted(r['details'], key=lambda x: x['est'], reverse=True):
                print(
                    f"  {d['q']:<40s} | rate=${d['rate']:>5.0f}/d | "
                    f"Q-share={d['q_share']:.3f}% | "
                    f"both={d['both']}/{d['cycles']} | "
                    f"reward=${d['est']:.4f}"
                )


# ═══════════════════════════════════════════════════════════════════════
# 5. MAIN
# ═══════════════════════════════════════════════════════════════════════

def parse_duration(s: str) -> int:
    s = s.strip().lower()
    if s.endswith("h"):
        return int(float(s[:-1]) * 3600)
    if s.endswith("m"):
        return int(float(s[:-1]) * 60)
    return int(s.rstrip("s"))


def create_client():
    """Create the real CLOB client for read-only market data."""
    from config import (
        CLOB_API_KEY, CLOB_SECRET, CLOB_PASS_PHRASE,
        HOST, PRIVATE_KEY, CHAIN_ID, SIGNATURE_TYPE, FUNDER,
    )
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from rate_limiter import RateLimitedClient

    creds = ApiCreds(api_key=CLOB_API_KEY, api_secret=CLOB_SECRET, api_passphrase=CLOB_PASS_PHRASE)
    raw = ClobClient(
        host=HOST, key=PRIVATE_KEY, chain_id=CHAIN_ID,
        signature_type=SIGNATURE_TYPE, funder=FUNDER, creds=creds,
    )
    return RateLimitedClient(raw)


def fetch_all_reward_markets() -> list[dict]:
    """Fetch ALL reward markets from the CLOB endpoint + Gamma details.

    The CLOB endpoint has 4700+ reward markets with authoritative
    min_size, max_spread, and daily_rate. Gamma provides question text,
    token IDs, liquidity, volume, and prices.

    Returns list of market dicts compatible with run_cycle().
    """
    import requests

    # Step 1: Fetch ALL CLOB reward markets (paginated)
    log.info("  Fetching CLOB rewards (authoritative source)...")
    clob_markets = []
    cursor = ""
    for _ in range(20):
        params = {"limit": 500}
        if cursor:
            params["next_cursor"] = cursor
        try:
            resp = requests.get(
                "https://clob.polymarket.com/rewards/markets/current",
                params=params, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning(f"  CLOB rewards fetch failed: {e}")
            break
        items = data.get("data", [])
        clob_markets.extend(items)
        cursor = data.get("next_cursor", "")
        if not cursor or not items:
            break
    log.info(f"  CLOB: {len(clob_markets)} reward markets")

    # Step 2: Fetch Gamma markets for details (paginated)
    log.info("  Fetching Gamma market details...")
    gamma_all = []
    for offset in range(0, 2000, 100):
        try:
            resp = requests.get(
                "https://gamma-api.polymarket.com/markets",
                params={"limit": 100, "offset": offset, "closed": "false"},
                timeout=15,
            )
            batch = resp.json()
        except Exception:
            break
        if not batch:
            break
        gamma_all.extend(batch)
    log.info(f"  Gamma: {len(gamma_all)} markets")

    gamma_by_cid = {m.get("conditionId", ""): m for m in gamma_all}

    # Step 3: Merge
    merged = []
    for c in clob_markets:
        cid = c["condition_id"]
        rate = float(c.get("total_daily_rate") or 0)
        if rate < 1:
            continue
        min_size = float(c.get("rewards_min_size") or 50)
        ms_cents = float(c.get("rewards_max_spread") or 4.5)

        g = gamma_by_cid.get(cid)
        if not g:
            continue

        # Parse token IDs
        try:
            token_ids = json.loads(g.get("clobTokenIds") or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        if len(token_ids) < 2:
            continue

        # Parse prices
        yes_price = None
        try:
            prices = json.loads(g.get("outcomePrices") or "[]")
            yes_price = float(prices[0]) if prices else None
        except Exception:
            pass

        liq = float(g.get("liquidityNum") or 0)
        vol = float(g.get("volume24hrClob") or 0)

        merged.append({
            "condition_id": cid,
            "question": g.get("question", ""),
            "token_ids": token_ids,
            "yes_price": yes_price,
            "daily_rate": rate,
            "min_size": min_size,
            "max_spread": ms_cents / 100.0,  # cents → price units
            "tick_size": float(g.get("orderPriceMinTickSize") or 0.01),
            "liquidity": liq,
            "volume_24h": vol,
        })

    # Sort by liquidity ascending (lowest competition first)
    merged.sort(key=lambda x: x["liquidity"])
    log.info(f"  Merged: {len(merged)} markets with rate >= $1/day")
    return merged


def main():
    parser = argparse.ArgumentParser(description="Paper Trading Simulator v2")
    parser.add_argument("--duration", default="1h", help="e.g. 10m, 1h, 6h")
    parser.add_argument("--balance", type=float, default=3300, help="USDC per strategy")
    args = parser.parse_args()

    duration_secs = parse_duration(args.duration)
    balance = args.balance

    log.info(f"Paper Trading v2 starting")
    log.info(f"  Duration: {args.duration} ({duration_secs}s)")
    log.info(f"  Balance: ${balance:.0f} per strategy")
    log.info(f"  Strategies: {[s.name for s in STRATEGIES]}")

    # Create real client + book cache
    real_client = create_client()
    from paper_client import CachedOrderBookProvider
    book_cache = CachedOrderBookProvider(real_client, ttl_secs=25.0)

    # Fetch ALL reward markets from CLOB + Gamma
    log.info("Fetching reward markets...")
    try:
        all_markets = fetch_all_reward_markets()
    except Exception as e:
        log.error(f"Failed to fetch markets: {e}")
        sys.exit(1)

    if not all_markets:
        log.error("No eligible markets found. Exiting.")
        sys.exit(1)

    # Log market summary (lowest liquidity first = best farming targets)
    low_comp = [m for m in all_markets if m["daily_rate"] >= 10]
    log.info(f"Markets with rate >= $10/day: {len(low_comp)}")
    for i, m in enumerate(low_comp[:15]):
        log.info(
            f"  #{i+1} {m['question'][:45]} | rate=${m['daily_rate']:.0f}/d | "
            f"liq=${m['liquidity']:.0f} | spread={m['max_spread']:.3f} | min_sz={m['min_size']:.0f}"
        )

    # Create sessions
    sessions = [Session(strat, balance, real_client, book_cache) for strat in STRATEGIES]
    log.info(f"Initialized {len(sessions)} sessions")

    # Shutdown handler
    shutdown = False
    def _sig(signum, frame):
        nonlocal shutdown
        shutdown = True
        log.info("Shutdown requested...")
    signal.signal(signal.SIGINT, _sig)

    # Main loop
    start = time.time()
    cycle = 0
    last_market_refresh = time.time()
    last_status = time.time()

    import config
    cycle_secs = getattr(config, "ORDER_REFRESH_SECS", 30)
    refresh_secs = getattr(config, "MARKET_REFRESH_SECS", 1800)

    log.info("Starting cycles...")

    while not shutdown and (time.time() - start) < duration_secs:
        cycle += 1
        t0 = time.time()

        # Refresh markets
        if time.time() - last_market_refresh >= refresh_secs:
            try:
                book_cache.invalidate()
                all_markets = fetch_all_reward_markets()
                last_market_refresh = time.time()
                log.info(f"Market refresh: {len(all_markets)} markets")
            except Exception as e:
                log.warning(f"Market refresh failed: {e}")

        # Run each session
        for session in sessions:
            try:
                run_cycle(session, all_markets)
            except Exception as e:
                log.error(f"[{session.name}] Cycle error: {e}")

        # Status every 5 minutes
        if time.time() - last_status >= 300:
            elapsed = (time.time() - start) / 60
            remaining = (duration_secs - (time.time() - start)) / 60
            balances = " | ".join(f"{s.name}=${s.pc._usdc_balance:.0f}" for s in sessions)
            log.info(f"Cycle {cycle} | {elapsed:.0f}m elapsed, {remaining:.0f}m left | {balances}")
            last_status = time.time()

        # Sleep until next cycle
        elapsed_cycle = time.time() - t0
        sleep_time = max(0, cycle_secs - elapsed_cycle)
        if sleep_time > 0 and not shutdown:
            time.sleep(sleep_time)

    # Final results
    log.info("Paper trading complete. Collecting results...")
    duration_hrs = (time.time() - start) / 3600

    results = [get_results(s) for s in sessions]
    print_results(results, duration_hrs)


if __name__ == "__main__":
    main()
