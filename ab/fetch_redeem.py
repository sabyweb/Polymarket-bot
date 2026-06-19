#!/usr/bin/env python3
"""ab/fetch_redeem.py — one-time READ-ONLY pull of resolution proceeds (data-api /activity) -> cache JSON.

Public endpoint, no auth, no trading state touched (within the read-only-on-live invariant; the bot's
own reconciler/soak_monitor read the same feed). Cached into the snapshot dir so the held-to-resolution
net is reproducible offline afterward. A browser User-Agent is required (bare urllib gets a 403 WAF block).

Usage: python3 -m ab.fetch_redeem [--type REDEEM] [--snap snapshots/2026-06-19]
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import time
import urllib.parse
import urllib.request

DATA_API = "https://data-api.polymarket.com/activity"
FUNDER = "0xB23Bc80E6719099aeBE0c34389f05EC8C928503f"  # verified: returns our markets' conditionIds
_UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
       "Accept": "application/json"}
DEFAULT_SNAP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "snapshots", "2026-06-19")


def fetch(funder: str, ptype: str = "REDEEM", max_pages: int = 80) -> list:
    out, offset = [], 0
    for _ in range(max_pages):
        url = DATA_API + "?" + urllib.parse.urlencode(
            {"user": funder, "type": ptype, "limit": 500, "offset": offset})
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            page = json.load(r)
        if not page:
            break
        out.extend(page)
        if len(page) < 500:
            break
        offset += 500
        time.sleep(0.2)
    return out


def main():
    ap = argparse.ArgumentParser(description="Read-only pull of resolution proceeds -> cache JSON.")
    ap.add_argument("--snap", default=DEFAULT_SNAP)
    ap.add_argument("--funder", default=FUNDER)
    ap.add_argument("--type", default="REDEEM")
    args = ap.parse_args()

    data = fetch(args.funder, args.type)
    path = os.path.join(args.snap, f"{args.type.lower()}_activity.json")
    with open(path, "w") as f:
        json.dump(data, f)

    by_cid = {}
    for it in data:
        c = it.get("conditionId", "")
        if c:
            by_cid[c] = by_cid.get(c, 0.0) + float(it.get("usdcSize", 0) or 0)
    zero = sum(1 for c, v in by_cid.items() if v == 0)
    ts = [float(it.get("timestamp", 0) or 0) for it in data if it.get("timestamp")]
    print(f"[fetch_redeem] {args.type}: {len(data)} events, {len(by_cid)} cids -> {path}")
    print(f"  total proceeds usdcSize: ${sum(by_cid.values()):,.2f}   cids with $0 proceeds (lost side held): {zero}")
    if ts:
        print(f"  event date range: {_dt.datetime.utcfromtimestamp(min(ts)):%Y-%m-%d} .. "
              f"{_dt.datetime.utcfromtimestamp(max(ts)):%Y-%m-%d}")


if __name__ == "__main__":
    main()
