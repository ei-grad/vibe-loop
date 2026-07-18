from pathlib import Path
import unittest


class SelectedStoryTests(unittest.TestCase):
    def test_selected_story_is_recorded(self) -> None:
        content = Path("docs/selected-story.md").read_text(encoding="utf-8")
        self.assertIn("checkout:T002", content)
        self.assertIn("checkout story", content)


if __name__ == "__main__":
    unittest.main()
