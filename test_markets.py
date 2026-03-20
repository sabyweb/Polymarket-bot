"""
Standalone market discovery test.

Fetches and scores all eligible markets, then prints a ranked summary.
Useful for inspecting which markets the bot would select without
actually starting the trading loop.

Usage:
    python test_markets.py
"""

import logging
from market import get_rewards_markets


def main() -> None:
    """Fetch, score, and display the top reward markets."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    markets = get_rewards_markets()

    if not markets:
        print("\nNo suitable markets found.")
        return

    print(f"\n{'=' * 60}")
    print(f"TOP {len(markets)} MARKETS")
    print(f"{'=' * 60}\n")

    for i, m in enumerate(markets, 1):
        days = f"{m['days_left']:.1f}" if m["days_left"] else "Unknown"
        print(f"#{i}  {m['question']}")
        print(f"    Score:       {m['score']}/100")
        print(f"    Yes Price:   {m['yes_price']}")
        print(f"    Daily Rate:  ${m['daily_rate']:.2f}/day")
        print(f"    Min Size:    {m['min_size']} shares")
        print(f"    Max Spread:  {m['max_spread'] * 100:.1f}c")
        print(f"    Tick Size:   {m['tick_size']}")
        print(f"    Days Left:   {days}")
        print(f"    Liquidity:   ${m['liquidity']:,.0f}")
        print()


if __name__ == "__main__":
    main()
