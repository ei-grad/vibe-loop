from pathlib import Path
import unittest


class SelectedStoryTests(unittest.TestCase):
    def test_selected_story_is_recorded(self) -> None:
        content = Path("docs/selected-story.md").read_text(encoding="utf-8")
        self.assertIn("checkout-mutation:1.2", content)
        self.assertIn("idempotency", content)


if __name__ == "__main__":
    unittest.main()
