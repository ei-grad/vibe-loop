from __future__ import annotations

import unittest

from demo_math import normalize_slug


class NormalizeSlugTests(unittest.TestCase):
    def test_normalizes_whitespace(self) -> None:
        self.assertEqual(normalize_slug("Hello World"), "hello-world")


if __name__ == "__main__":
    unittest.main()
