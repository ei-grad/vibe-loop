from __future__ import annotations

import unittest

from runtime_demo import count_lines


class CountLinesTests(unittest.TestCase):
    def test_ignores_blank_lines(self) -> None:
        self.assertEqual(count_lines("alpha\n\nbeta\n"), 2)


if __name__ == "__main__":
    unittest.main()
