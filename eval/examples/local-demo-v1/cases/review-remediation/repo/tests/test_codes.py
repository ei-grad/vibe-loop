from __future__ import annotations

import unittest

from review_rules import is_valid_code


class CodeValidationTests(unittest.TestCase):
    def test_accepts_basic_code(self) -> None:
        self.assertTrue(is_valid_code("AB-12"))

    def test_rejects_empty_code(self) -> None:
        self.assertFalse(is_valid_code(""))


if __name__ == "__main__":
    unittest.main()
