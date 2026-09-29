"""Cart totals with promotional discount codes."""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
CODES = {"SAVE10": Decimal("0.10"), "SAVE25": Decimal("0.25")}


def line_total(unit_price: Decimal, quantity: int) -> Decimal:
    if quantity < 1:
        raise ValueError("quantity must be at least 1")
    return unit_price * quantity


def total(lines: list[tuple[Decimal, int]], code: str | None = None) -> Decimal:
    """Sum of line totals, less any discount code, rounded half-up to the cent."""
    subtotal = sum((line_total(price, qty) for price, qty in lines), Decimal("0"))
    if code is not None:
        if code not in CODES:
            raise ValueError(f"unknown discount code {code!r}")
        subtotal -= subtotal * CODES[code]
    return subtotal.quantize(CENT, rounding=ROUND_HALF_UP)
