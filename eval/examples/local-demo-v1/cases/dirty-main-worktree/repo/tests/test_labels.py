from __future__ import annotations

import unittest

from dirty_demo import clean_label


class CleanLabelTests(unittest.TestCase):
    def test_trims_before_title_casing(self) -> None:
        self.assertEqual(clean_label("  support queue  "), "Support Queue")


if __name__ == "__main__":
    unittest.main()
