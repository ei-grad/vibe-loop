from pathlib import Path
import unittest


class SelectedStoryTests(unittest.TestCase):
    def test_selected_story_preserves_traceability(self) -> None:
        content = Path("docs/selected-story.md").read_text(encoding="utf-8")
        self.assertIn("LIST-02", content)
        self.assertIn("PRD-TSK-001", content)
        self.assertIn("PRD-TSK-002", content)


if __name__ == "__main__":
    unittest.main()
