"""Textual TUI for autopilot status.

This module is imported only when the user runs ``vibe-loop autopilot tui`` (or
in tests), so the optional ``textual`` dependency never burdens the core CLI.
The TUI is read-only: it consumes the structured autopilot status API and never
launches workers, mutates state, or exposes raw commands or secrets.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

from vibe_loop.autopilot import (
    AggregateProjectStatus,
    ProjectRegistry,
    collect_project_status,
    collect_registry_status,
)
from vibe_loop.config import load_config

TUI_COLUMNS = (
    "Project",
    "Repo",
    "Queue",
    "Workers",
    "Supervisor",
    "Log",
    "Last cycle",
    "Blockers",
    "Next wake",
)


def project_status_rows(
    results: list[AggregateProjectStatus],
) -> list[dict[str, str]]:
    """Project structured statuses into display rows.

    Rows are built from already-redacted ``ProjectStatus`` data, so no raw
    command strings or secrets are surfaced.
    """

    rows: list[dict[str, str]] = []
    for result in results:
        if result.status is None:
            rows.append(
                {
                    "project": result.name,
                    "repo": str(result.repo),
                    "queue": "—",
                    "workers": "—",
                    "supervisor": "—",
                    "log": "—",
                    "last_cycle": "—",
                    "blockers": f"error: {result.error}",
                    "next_wake": "—",
                }
            )
            continue
        status = result.status
        queue = status.queue
        queue_text = (
            "unavailable"
            if queue.source_error
            else f"{queue.runnable}/{queue.total} ready"
        )
        active_workers = sum(
            1 for worker in status.workers if worker.state == "running"
        )
        supervisor = status.supervisor
        supervisor_text = supervisor.state
        if supervisor.pid:
            supervisor_text += f" pid={supervisor.pid}"
        last_cycle = status.last_cycle
        last_cycle_text = (
            f"{last_cycle.cycle_id}: {last_cycle.status}"
            if last_cycle is not None
            else "—"
        )
        rows.append(
            {
                "project": result.name,
                "repo": str(result.repo),
                "queue": queue_text,
                "workers": str(active_workers),
                "supervisor": supervisor_text,
                "log": str(supervisor.log) if supervisor.log is not None else "—",
                "last_cycle": last_cycle_text,
                "blockers": ", ".join(status.blockers) if status.blockers else "none",
                "next_wake": status.next_wake or "—",
            }
        )
    return rows


def collect_tui_results(
    *,
    repo: Path | None = None,
    registry_path: Path | None = None,
) -> list[AggregateProjectStatus]:
    if registry_path is not None:
        return collect_registry_status(ProjectRegistry.load(registry_path))
    status = collect_project_status(load_config(repo or Path.cwd()))
    return [
        AggregateProjectStatus(
            name=status.display_name, repo=status.repo, status=status
        )
    ]


class AutopilotTUIApp(App):
    """A read-only project dashboard rendered from autopilot status rows."""

    TITLE = "vibe-loop autopilot"
    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, rows: list[dict[str, str]]) -> None:
        super().__init__()
        self._rows = rows

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="projects")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#projects", DataTable)
        table.cursor_type = "row"
        table.add_columns(*TUI_COLUMNS)
        for row in self._rows:
            table.add_row(
                row["project"],
                row["repo"],
                row["queue"],
                row["workers"],
                row["supervisor"],
                row["log"],
                row["last_cycle"],
                row["blockers"],
                row["next_wake"],
            )


def run_tui(
    *,
    repo: Path | None = None,
    registry_path: Path | None = None,
) -> None:
    results = collect_tui_results(repo=repo, registry_path=registry_path)
    AutopilotTUIApp(project_status_rows(results)).run()
