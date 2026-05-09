from __future__ import annotations

import json


PROFILE = {
    "kind": "markdown_headings",
    "source_paths": ["docs/roadmap.md"],
    "stable_ids": True,
    "fields": {
        "id": {
            "pattern": r"^(?P<id>[A-Z]+-[0-9]+):",
            "strategy": "heading_text",
        },
        "title": {
            "pattern": r"^[A-Z]+-[0-9]+:\s*(?P<title>.+)$",
            "strategy": "heading_text",
        },
        "status": {"label": "State"},
        "dependencies": {"label": "Depends", "none_values": ["none"]},
        "acceptance": {"label": "Acceptance"},
    },
    "status_map": {
        "done": ["Accepted"],
        "runnable": ["Ready"],
        "blocked": ["Blocked"],
    },
}


if __name__ == "__main__":
    print(json.dumps(PROFILE, sort_keys=True))
