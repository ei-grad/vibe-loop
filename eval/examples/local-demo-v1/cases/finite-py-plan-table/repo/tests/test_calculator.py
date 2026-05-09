from __future__ import annotations

import unittest

from finite_math import loyalty_total


class LoyaltyTotalTests(unittest.TestCase):
    def test_member_receives_ten_unit_discount(self) -> None:
        self.assertEqual(loyalty_total(100, member=True), 90)

    def test_non_member_pays_full_total(self) -> None:
        self.assertEqual(loyalty_total(100, member=False), 100)


if __name__ == "__main__":
    unittest.main()
