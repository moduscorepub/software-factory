from decimal import Decimal

import pytest

from pricing.cart import total


def test_sums_lines():
    assert total([(Decimal("2.50"), 2), (Decimal("1.00"), 1)]) == Decimal("6.00")


def test_discount_code():
    assert total([(Decimal("10.00"), 1)], "SAVE10") == Decimal("9.00")


def test_unknown_code_rejected():
    with pytest.raises(ValueError):
        total([(Decimal("1.00"), 1)], "BOGUS")
