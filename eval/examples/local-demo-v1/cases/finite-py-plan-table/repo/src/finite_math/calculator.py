from __future__ import annotations


def loyalty_total(subtotal: int, *, member: bool) -> int:
    discount = 5 if member else 0
    return subtotal - discount
