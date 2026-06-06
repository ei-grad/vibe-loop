from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from vibe_loop.webui import (
    autopilot_status_payload,
    build_server,
    render_status_page,
    status_response,
)

SAMPLE_PAYLOAD: dict[str, object] = {
    "schema_version": 1,
    "generated_at": "now",
    "mode": "repo",
    "projects": [
        {"name": "alpha", "repo": "/repos/alpha", "status": {"queue": {}}, "error": ""}
    ],
}


class WebuiPayloadTests(unittest.TestCase):
    def test_payload_maps_repo_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(repo, "TASK-01", "Next")
            commit_all(repo)
            payload = autopilot_status_payload(repo=repo, generated_at="now")

        self.assertEqual(payload["mode"], "repo")
        self.assertEqual(len(payload["projects"]), 1)
        project = payload["projects"][0]
        self.assertEqual(project["status"]["queue"]["runnable"], 1)
        # Cycle history is exposed in the structured payload (empty before any run).
        self.assertEqual(project["recent_cycles"], [])
        # Agent config is self-redacted in ProjectStatus; no raw command leaks.
        self.assertNotIn("{prompt}", json.dumps(payload))

    def test_payload_exposes_recorded_cycle_history(self) -> None:
        from vibe_loop.runs import AUTOPILOT_CYCLE_RECORD_TYPE, RunStore

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            init_repo(repo)
            write_plan(repo, "TASK-01", "Next")
            commit_all(repo)
            run_store = RunStore(repo / ".vibe-loop" / "runs.jsonl")
            for index in (1, 2):
                run_store.append_record(
                    {
                        "schema_version": 1,
                        "record_type": AUTOPILOT_CYCLE_RECORD_TYPE,
                        "cycle_id": f"cycle-{index}",
                        "status": "idle",
                        "occurred_at": f"2026-06-06T00:0{index}:00+00:00",
                    }
                )
            payload = autopilot_status_payload(repo=repo, generated_at="now")

        cycles = payload["projects"][0]["recent_cycles"]
        self.assertEqual(
            [cycle["cycle_id"] for cycle in cycles], ["cycle-1", "cycle-2"]
        )

    def test_status_response_routes_page_api_and_unknown(self) -> None:
        page_code, page_type, page_body = status_response("/", lambda: SAMPLE_PAYLOAD)
        api_code, api_type, api_body = status_response(
            "/api/status", lambda: SAMPLE_PAYLOAD
        )
        missing_code, _, _ = status_response("/etc/passwd", lambda: SAMPLE_PAYLOAD)

        self.assertEqual(page_code, 200)
        self.assertIn("text/html", page_type)
        self.assertIn(b"<table", page_body)
        self.assertEqual(api_code, 200)
        self.assertEqual(api_type, "application/json")
        self.assertEqual(json.loads(api_body)["mode"], "repo")
        self.assertEqual(missing_code, 404)

    def test_status_page_holds_no_project_data(self) -> None:
        # The page must fetch the API rather than embed data, so the server stays
        # the single source of truth (no text scraping).
        page = render_status_page()
        self.assertIn("fetch('api/status')", page)
        self.assertNotIn("alpha", page)


class WebuiHttpSmokeTests(unittest.TestCase):
    def test_server_serves_status_and_rejects_writes(self) -> None:
        server = build_server(lambda: SAMPLE_PAYLOAD, host="127.0.0.1", port=0)
        host, port = server.server_address[0], server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://{host}:{port}"
        try:
            with urllib.request.urlopen(f"{base}/api/status", timeout=5) as response:
                api = json.loads(response.read())
            with urllib.request.urlopen(f"{base}/", timeout=5) as response:
                page = response.read()
            write_status = None
            try:
                urllib.request.urlopen(
                    urllib.request.Request(
                        f"{base}/api/status", method="POST", data=b"x"
                    ),
                    timeout=5,
                )
            except urllib.error.HTTPError as exc:
                write_status = exc.code
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.assertEqual(api["projects"][0]["name"], "alpha")
        self.assertIn(b"<table", page)
        # A read-only surface rejects writes (stdlib handler answers 501).
        self.assertIn(write_status, {400, 405, 501})


def init_repo(repo: Path) -> None:
    run(repo, "git", "init", "-b", "main")
    run(repo, "git", "config", "user.email", "test@example.com")
    run(repo, "git", "config", "user.name", "Test User")


def write_plan(repo: Path, task_id: str, status: str) -> None:
    (repo / "PLAN.md").write_text(
        "# Plan\n\n"
        "| ID | Priority | Status | Dependencies | Scope | Acceptance | Evidence |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"| {task_id} | P0 | {status} | none | scope | works | tests |\n",
        encoding="utf-8",
    )


def commit_all(repo: Path) -> None:
    run(repo, "git", "add", "PLAN.md")
    run(repo, "git", "commit", "-m", "initial")


def run(repo: Path, *args: str) -> None:
    subprocess.run(
        args, cwd=repo, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


if __name__ == "__main__":
    unittest.main()
