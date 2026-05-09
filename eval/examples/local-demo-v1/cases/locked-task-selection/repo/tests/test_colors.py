from __future__ import annotations

import unittest

from selection_demo import color_name


class ColorNameTests(unittest.TestCase):
    def test_uses_known_aliases(self) -> None:
        self.assertEqual(color_name("R"), "red")
        self.assertEqual(color_name("B"), "blue")
        self.assertEqual(color_name("x"), "unknown")


if __name__ == "__main__":
    unittest.main()
