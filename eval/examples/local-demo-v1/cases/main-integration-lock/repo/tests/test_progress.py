from __future__ import annotations

import unittest

from mil_demo import clamp_percent


class ClampPercentTests(unittest.TestCase):
    def test_clamps_to_bounds(self) -> None:
        self.assertEqual(clamp_percent(-5), 0)
        self.assertEqual(clamp_percent(105), 100)
        self.assertEqual(clamp_percent(42), 42)


if __name__ == "__main__":
    unittest.main()
