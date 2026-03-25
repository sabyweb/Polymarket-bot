"""
Price conversion helpers for the Polymarket market-making bot.

All prices in the bot are stored in YES-equivalent terms. When placing
or evaluating orders on the CLOB, they must be converted to the actual
CLOB price for the specific side:

    YES CLOB price = YES-equivalent price
    NO CLOB price  = 1 - YES-equivalent price

These helpers make every conversion explicit and self-documenting,
replacing bare `1 - price` patterns that have historically caused bugs.
They also include validation to catch invalid prices early.
"""


def to_clob(yes_equiv: float, side: str) -> float:
    """Convert a YES-equivalent price to the actual CLOB price.

    Args:
        yes_equiv: Price in YES-equivalent terms (0.0 to 1.0).
        side: "yes" or "no".

    Returns:
        The price to use on the CLOB for that side.

    Examples:
        to_clob(0.30, "yes") → 0.30  (buy YES at 30c)
        to_clob(0.60, "no")  → 0.40  (buy NO at 40c, since YES ask is 60c)
    """
    if not (0 <= yes_equiv <= 1):
        raise ValueError(f"YES-equiv price must be 0-1, got {yes_equiv}")
    return yes_equiv if side == "yes" else (1 - yes_equiv)


def to_yes_equiv(clob_price: float, side: str) -> float:
    """Convert a CLOB price to YES-equivalent terms.

    Args:
        clob_price: Price on the CLOB (0.0 to 1.0).
        side: "yes" or "no".

    Returns:
        The price in YES-equivalent terms.

    Examples:
        to_yes_equiv(0.30, "yes") → 0.30
        to_yes_equiv(0.40, "no")  → 0.60  (NO at 40c CLOB = YES at 60c)
    """
    if not (0 <= clob_price <= 1):
        raise ValueError(f"CLOB price must be 0-1, got {clob_price}")
    return clob_price if side == "yes" else (1 - clob_price)


def clob_cost(yes_equiv: float, side: str, shares: float) -> float:
    """Compute the USD cost of buying shares at a YES-equivalent price.

    Args:
        yes_equiv: Price in YES-equivalent terms.
        side: "yes" or "no".
        shares: Number of shares.

    Returns:
        Total USD cost.

    Examples:
        clob_cost(0.30, "yes", 100) → 30.0  (100 YES shares at 30c)
        clob_cost(0.60, "no", 200)  → 80.0  (200 NO shares at 40c CLOB)
    """
    return shares * to_clob(yes_equiv, side)
