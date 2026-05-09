from __future__ import annotations

import unittest

from roadmap_demo import route_key


class RouteKeyTests(unittest.TestCase):
    def test_collapses_repeated_separators(self) -> None:
        self.assertEqual(route_key("Profile   Settings"), "profile-settings")
        self.assertEqual(route_key("Profile---Settings"), "profile-settings")


if __name__ == "__main__":
    unittest.main()
