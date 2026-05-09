from __future__ import annotations

import unittest

from main_advanced_demo import parse_numbers


class ParseNumbersTests(unittest.TestCase):
    def test_ignores_blank_items(self) -> None:
        self.assertEqual(parse_numbers("1, 2,,3,"), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
