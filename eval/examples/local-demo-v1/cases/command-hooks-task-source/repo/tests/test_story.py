import json
from pathlib import Path
import unittest


class CommandStoryTests(unittest.TestCase):
    def test_authoritative_task_and_story_are_complete(self) -> None:
        tasks = json.loads(Path("tasks.json").read_text(encoding="utf-8"))["tasks"]
        by_id = {task["id"]: task for task in tasks}
        self.assertEqual(by_id["HOOK-02"]["status"], "Done")
        self.assertEqual(by_id["HOOK-03"]["status"], "Planned")
        content = Path("docs/selected-story.md").read_text(encoding="utf-8")
        self.assertIn("HOOK-02", content)
        self.assertIn("PRD-TSK-003", content)
        self.assertIn("PRD-WRK-011", content)


if __name__ == "__main__":
    unittest.main()
