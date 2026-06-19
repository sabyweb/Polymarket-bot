#!/usr/bin/env python3
"""ab/halt_diagnose.py — Halt-Doctor DIAGNOSIS engine (read-only, SAFE).  [diagnosis half only]

On a halt, re-derive the fired kill from AUTHORITATIVE ground truth and classify it. This module
NEVER clears a kill and NEVER restarts anything — it is the "feedback loop of what went wrong".
The auto-recovery half is separate and gated on a recorded operator authorization.

Verdicts (fail-safe: anything not POSITIVELY proven false => REAL/UNCERTAIN => escalate):
  FALSE_POSITIVE — claimed metric is contradicted by authoritative ground truth AND the trip is RECENT
                   (now ~= trip) AND matches a known false signature (e.g., a DB-missed on-chain
                   position making the portfolio read too low). ONLY this is auto-recovery-eligible.
  REAL_ACTIVE    — authoritative ground truth currently confirms the kill. Escalate; do NOT resume.
  REAL_RESOLVED  — real at trip, condition has since cleared. Escalate; human may resume WITH headroom.
  UNCERTAIN      — parser miss or data source down. Escalate.

The cardinal rule is preserved: a REAL kill always escalates to a human, even when it has since
cleared (REAL_RESOLVED) — diagnosis informs the human, it does not auto-clear a real kill.
"""
from __future__ import annotations

import argparse
import json
import re
import urllib.parse
import urllib.request

DATA_API_POSITIONS = "https://data-api.polymarket.com/positions"
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
       "Accept": "application/json"}

# false-positive eligibility: trip must be recent (now ~= trip) so claimed-vs-current is a fair check
RECENT_TRIP_SEC = 1800.0
PORTFOLIO_FP_TOL = 0.05  # claimed portfolio must be >5% below authoritative to suspect a missed position

_DRAWDOWN_RE = re.compile(
    r"drawdown\s+([\d.]+)%\s*>\s*([\d.]+)%.*?peak\s*\$?([\d.]+).*?portfolio=\$?([\d.]+)", re.I)


def parse_kill_reason(reason: str) -> dict:
    r = reason or ""
    low = r.lower()
    if "drawdown" in low:
        m = _DRAWDOWN_RE.search(r)
        if m:
            return dict(kill_type="drawdown", claimed_dd_pct=float(m.group(1)),
                        threshold_pct=float(m.group(2)), peak=float(m.group(3)),
                        portfolio=float(m.group(4)))
        return dict(kill_type="drawdown", raw=r[:160])
    if "realized loss" in low or "realized_loss" in low:
        return dict(kill_type="realized_loss", raw=r[:160])
    if "fill_rate" in low or "fill rate" in low:
        return dict(kill_type="fill_rate", raw=r[:160])
    if "burst" in low or "rapid" in low:
        return dict(kill_type="rapid_growth", raw=r[:160])
    if "unrealized" in low:
        return dict(kill_type="unrealized_loss", raw=r[:160])
    return dict(kill_type="unknown", raw=r[:160])


def classify_drawdown(claimed: dict, auth: dict, trip_age_sec) -> tuple[str, str, str]:
    ap, pk = auth.get("portfolio"), auth.get("peak")
    thr = (claimed.get("threshold_pct") or 20.0) / 100.0
    if ap is None or pk is None or pk <= 0:
        return "UNCERTAIN", "authoritative portfolio/peak unavailable", "escalate — cannot verify"
    auth_dd = 1.0 - ap / pk
    cp = claimed.get("portfolio")
    # FALSE_POSITIVE: recent trip + claimed portfolio grossly BELOW authoritative => kill read a
    # missed/stale on-chain position too low (the verified 2026-06-13 DB-miss deadlock signature).
    if (cp is not None and trip_age_sec is not None and trip_age_sec <= RECENT_TRIP_SEC
            and ap > cp * (1 + PORTFOLIO_FP_TOL) and auth_dd < thr):
        return ("FALSE_POSITIVE",
                f"claimed portfolio ${cp:.2f} but authoritative ${ap:.2f} (+${ap - cp:.2f}); "
                f"auth drawdown {auth_dd * 100:.2f}% < {thr * 100:.0f}% — kill used a too-low portfolio",
                "eligible for GATED auto-recovery (whitelisted DB-miss signature)")
    if auth_dd >= thr:
        return ("REAL_ACTIVE",
                f"authoritative drawdown {auth_dd * 100:.2f}% >= {thr * 100:.0f}% "
                f"(portfolio ${ap:.2f}, peak ${pk:.2f})",
                "escalate — do NOT resume; drawdown genuinely breached")
    return ("REAL_RESOLVED",
            f"authoritative drawdown {auth_dd * 100:.2f}% < {thr * 100:.0f}% now "
            f"(portfolio ${ap:.2f}, peak ${pk:.2f}); was real at trip",
            "escalate — condition cleared; human may resume WITH headroom (peak is stale-era; reset baseline)")


def diagnose(reason: str, auth: dict, trip_age_sec=None) -> dict:
    parsed = parse_kill_reason(reason)
    kt = parsed["kill_type"]
    if kt == "drawdown" and "portfolio" in parsed:
        verdict, evidence, action = classify_drawdown(parsed, auth, trip_age_sec)
    elif kt == "realized_loss":
        rl, wal = auth.get("realized_loss_24h"), auth.get("portfolio")
        if rl is None or wal is None:
            verdict, evidence, action = "UNCERTAIN", "authoritative realized loss unavailable", "escalate"
        elif rl > 0.10 * wal:
            verdict, evidence, action = "REAL_ACTIVE", f"24h realized loss ${rl:.2f} > 10% of ${wal:.2f}", "escalate — do NOT resume"
        else:
            verdict, evidence, action = "REAL_RESOLVED", f"24h realized loss ${rl:.2f} <= 10% now", "escalate — cleared; human may resume"
    else:
        verdict, evidence, action = ("UNCERTAIN",
                                     f"kill_type={kt}: not auto-classifiable read-only (transient/rate kill or unparsed)",
                                     "escalate to human")
    return dict(kill_type=kt, verdict=verdict, evidence=evidence, recommended_action=action,
                claimed=parsed, authoritative=auth, trip_age_sec=trip_age_sec)


def fetch_inventory(funder: str):
    """Authoritative on-chain marked inventory value = Σ size*curPrice over /positions. None on failure."""
    url = DATA_API_POSITIONS + "?" + urllib.parse.urlencode({"user": funder, "sizeThreshold": "0.1"})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=25) as r:
            data = json.load(r)
        if not isinstance(data, list):
            return None
        return sum(float(p.get("size", 0) or 0) * float(p.get("curPrice", 0) or 0) for p in data)
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(description="Halt-Doctor diagnosis (read-only).")
    ap.add_argument("--reason", required=True, help="the fired kill reason string (from the farmer journal/alloc)")
    ap.add_argument("--cash", type=float, required=True, help="authoritative current cash (USD)")
    ap.add_argument("--peak", type=float, required=True, help="peak total_value from portfolio_snapshots")
    ap.add_argument("--funder", default="0xB23Bc80E6719099aeBE0c34389f05EC8C928503f")
    ap.add_argument("--trip-age-sec", type=float, default=None, help="seconds since the ORIGINAL trip")
    ap.add_argument("--realized-loss-24h", type=float, default=None)
    args = ap.parse_args()

    inv = fetch_inventory(args.funder)
    portfolio = args.cash + inv if inv is not None else None
    auth = dict(portfolio=portfolio, peak=args.peak, cash=args.cash, inventory=inv,
                realized_loss_24h=args.realized_loss_24h)
    d = diagnose(args.reason, auth, args.trip_age_sec)

    print("# Halt-Doctor diagnosis (read-only — never clears a kill, never restarts)")
    print(f"  kill_type : {d['kill_type']}")
    print(f"  authoritative: portfolio={('$%.2f' % portfolio) if portfolio is not None else 'UNAVAILABLE'} "
          f"(cash ${args.cash:.2f} + inventory {('$%.2f' % inv) if inv is not None else 'n/a'})  peak ${args.peak:.2f}")
    print(f"  VERDICT   : {d['verdict']}")
    print(f"  evidence  : {d['evidence']}")
    print(f"  action    : {d['recommended_action']}")
    if d["verdict"] != "FALSE_POSITIVE":
        print("  -> NOT auto-recovery-eligible: escalates to human (cardinal rule).")


if __name__ == "__main__":
    main()
