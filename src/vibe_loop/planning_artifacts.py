from __future__ import annotations

import dataclasses
import html
import json
import shlex
from datetime import datetime
from pathlib import Path

from vibe_loop.config import VibeConfig, planning_analytics_output_report
from vibe_loop.planning_evidence import DEFAULT_GIT_COMMIT_LIMIT
from vibe_loop.planning_timeline import (
    PLANNING_TIMELINE_SCHEMA_VERSION,
    build_planning_timeline,
)


PLANNING_ARTIFACTS_COMMAND = "vibe-loop planning artifacts"
PLANNING_GANTT_SCHEMA_VERSION = 1
GANTT_MARKER_PREFIX = "<!-- vibe-loop-planning-gantt "
GANTT_MARKER_SUFFIX = " -->"


@dataclasses.dataclass(frozen=True)
class PlanningArtifactPaths:
    timeline_json: Path
    timeline_json_source: str
    gantt_html: Path
    gantt_html_source: str

    def to_json(self) -> dict[str, object]:
        return {
            "timeline_json": {
                "path": str(self.timeline_json),
                "source": self.timeline_json_source,
            },
            "gantt_html": {
                "path": str(self.gantt_html),
                "source": self.gantt_html_source,
            },
        }


@dataclasses.dataclass(frozen=True)
class PlanningArtifactBundle:
    paths: PlanningArtifactPaths
    timeline: dict[str, object]
    gantt_html: str

    @property
    def warning_count(self) -> int:
        warnings = self.timeline.get("warnings", [])
        if isinstance(warnings, list):
            return len(warnings)
        return 0


def planning_artifact_paths(
    config: VibeConfig,
    *,
    output: Path | None = None,
    html_output: Path | None = None,
) -> PlanningArtifactPaths:
    configured = planning_analytics_output_report(config)
    if output is None:
        timeline_path = Path(str(configured["timeline_json"]["path"]))
        timeline_source = str(configured["timeline_json"]["source"])
    else:
        timeline_path = repo_relative_output_path(
            config.repo,
            output,
            "planning artifacts --output",
        )
        timeline_source = "cli"
    if html_output is None:
        gantt_path = Path(str(configured["gantt_html"]["path"]))
        gantt_source = str(configured["gantt_html"]["source"])
    else:
        gantt_path = repo_relative_output_path(
            config.repo,
            html_output,
            "planning artifacts --html-output",
        )
        gantt_source = "cli"
    return PlanningArtifactPaths(
        timeline_json=timeline_path,
        timeline_json_source=timeline_source,
        gantt_html=gantt_path,
        gantt_html_source=gantt_source,
    )


def build_planning_artifact_bundle(
    config: VibeConfig,
    *,
    output: Path | None = None,
    html_output: Path | None = None,
    git_commit_limit: int = DEFAULT_GIT_COMMIT_LIMIT,
) -> PlanningArtifactBundle:
    paths = planning_artifact_paths(
        config,
        output=output,
        html_output=html_output,
    )
    artifact_config = config_with_cli_artifact_outputs(config, paths)
    timeline = build_planning_timeline(
        artifact_config,
        git_commit_limit=git_commit_limit,
    )
    return PlanningArtifactBundle(
        paths=paths,
        timeline=timeline,
        gantt_html=render_static_gantt_html(timeline),
    )


def write_planning_artifacts(bundle: PlanningArtifactBundle) -> None:
    bundle.paths.timeline_json.parent.mkdir(parents=True, exist_ok=True)
    bundle.paths.gantt_html.parent.mkdir(parents=True, exist_ok=True)
    bundle.paths.timeline_json.write_text(
        planning_timeline_artifact_json(bundle.timeline),
        encoding="utf-8",
    )
    bundle.paths.gantt_html.write_text(bundle.gantt_html, encoding="utf-8")


def check_planning_artifacts(bundle: PlanningArtifactBundle) -> list[str]:
    expected = (
        (
            bundle.paths.timeline_json,
            planning_timeline_artifact_json(bundle.timeline),
            "timeline JSON",
        ),
        (bundle.paths.gantt_html, bundle.gantt_html, "Gantt HTML"),
    )
    errors: list[str] = []
    for path, content, label in expected:
        if not path.exists():
            errors.append(f"{label} artifact is missing: {path}")
            continue
        if not path.is_file():
            errors.append(f"{label} artifact is not a file: {path}")
            continue
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"{label} artifact cannot be read: {path}: {exc}")
            continue
        if actual != content:
            errors.append(f"{label} artifact is stale: {path}")
    return errors


def inspect_planning_artifacts(
    config: VibeConfig,
    *,
    output: Path | None = None,
    html_output: Path | None = None,
) -> dict[str, object]:
    paths = planning_artifact_paths(config, output=output, html_output=html_output)
    timeline = inspect_timeline_artifact(
        paths.timeline_json,
        source=paths.timeline_json_source,
    )
    gantt = inspect_gantt_artifact(paths.gantt_html, source=paths.gantt_html_source)
    base_command = planning_artifacts_command(
        config,
        output=output,
        html_output=html_output,
    )
    next_commands = [base_command, f"{base_command} --check"]
    return {
        "timeline_json": timeline,
        "gantt_html": gantt,
        "next_repair_commands": next_commands,
    }


def inspect_timeline_artifact(path: Path, *, source: str) -> dict[str, object]:
    base = artifact_base(path, source=source)
    if not path.exists():
        return {
            **base,
            "exists": False,
            "freshness": "missing",
            "warning_count": None,
            "warnings": [],
        }
    if not path.is_file():
        return {
            **base,
            "exists": True,
            "freshness": "invalid",
            "error": "path is not a file",
            "warning_count": None,
            "warnings": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {
            **base,
            "exists": True,
            "freshness": "invalid",
            "error": str(exc),
            "warning_count": None,
            "warnings": [],
        }
    if not isinstance(payload, dict):
        return {
            **base,
            "exists": True,
            "freshness": "invalid",
            "error": "timeline artifact root is not an object",
            "warning_count": None,
            "warnings": [],
        }
    warnings = payload.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = []
    schema_status = (
        "current_schema"
        if payload.get("schema_version") == PLANNING_TIMELINE_SCHEMA_VERSION
        else "unknown_schema"
    )
    return {
        **base,
        "exists": True,
        "freshness": "not_checked",
        "schema_status": schema_status,
        "schema_version": payload.get("schema_version"),
        "generated_by": payload.get("generated_by"),
        "task_count": len(payload.get("tasks", []))
        if isinstance(payload.get("tasks"), list)
        else None,
        "warning_count": len(warnings),
        "warnings": warnings,
    }


def inspect_gantt_artifact(path: Path, *, source: str) -> dict[str, object]:
    base = artifact_base(path, source=source)
    if not path.exists():
        return {
            **base,
            "exists": False,
            "freshness": "missing",
            "warning_count": None,
        }
    if not path.is_file():
        return {
            **base,
            "exists": True,
            "freshness": "invalid",
            "error": "path is not a file",
            "warning_count": None,
        }
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            **base,
            "exists": True,
            "freshness": "invalid",
            "error": str(exc),
            "warning_count": None,
        }
    metadata = parse_gantt_metadata(text)
    if metadata is None:
        return {
            **base,
            "exists": True,
            "freshness": "unknown_schema",
            "warning_count": None,
        }
    schema_status = (
        "current_schema"
        if metadata.get("schema_version") == PLANNING_GANTT_SCHEMA_VERSION
        else "unknown_schema"
    )
    return {
        **metadata,
        **base,
        "exists": True,
        "freshness": "not_checked",
        "schema_status": schema_status,
    }


def artifact_base(path: Path, *, source: str) -> dict[str, object]:
    return {"path": str(path), "source": source}


def planning_timeline_artifact_json(timeline: dict[str, object]) -> str:
    return json.dumps(timeline, indent=2, sort_keys=True) + "\n"


def render_static_gantt_html(timeline: dict[str, object]) -> str:
    tasks = timeline_tasks(timeline)
    warnings = timeline_warnings(timeline)
    bars = gantt_bars(tasks)
    metadata = {
        "schema_version": PLANNING_GANTT_SCHEMA_VERSION,
        "generated_by": PLANNING_ARTIFACTS_COMMAND,
        "timeline_schema_version": timeline.get("schema_version"),
        "task_count": len(tasks),
        "warning_count": len(warnings),
    }
    marker = (
        GANTT_MARKER_PREFIX + json.dumps(metadata, sort_keys=True) + GANTT_MARKER_SUFFIX
    )
    min_start, max_end = chart_bounds(bars)
    summary = gantt_summary(tasks)
    return "\n".join(
        [
            marker,
            "<!doctype html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            f"<title>{escape_text('vibe-loop planning Gantt')}</title>",
            "<style>",
            STATIC_GANTT_CSS,
            "</style>",
            "</head>",
            "<body>",
            "<main>",
            "<header>",
            "<h1>Planning Gantt</h1>",
            "<dl>",
            f"<div><dt>Schedule policy</dt><dd>{escape_text(timeline.get('schedule_policy'))}</dd></div>",
            f"<div><dt>Tasks</dt><dd>{len(tasks)}</dd></div>",
            f"<div><dt>Actual</dt><dd>{summary['actual']}</dd></div>",
            f"<div><dt>Projected</dt><dd>{summary['projected']}</dd></div>",
            f"<div><dt>Blocked</dt><dd>{summary['blocked']}</dd></div>",
            f"<div><dt>Warnings</dt><dd>{len(warnings)}</dd></div>",
            "</dl>",
            "</header>",
            render_chart(bars, min_start, max_end),
            render_warning_section(warnings),
            render_task_table(tasks),
            "</main>",
            "</body>",
            "</html>",
            "",
        ]
    )


def render_chart(
    bars: list[dict[str, object]],
    min_start: datetime | None,
    max_end: datetime | None,
) -> str:
    if not bars or min_start is None or max_end is None:
        return (
            '<section class="chart-section">'
            "<h2>Timeline</h2>"
            '<p class="empty">No actual or projected spans are available.</p>'
            "</section>"
        )
    total_seconds = max((max_end - min_start).total_seconds(), 1)
    rows = [
        '<section class="chart-section">',
        "<h2>Timeline</h2>",
        '<div class="chart" role="list">',
    ]
    for bar in bars:
        start = bar["start"]
        end = bar["end"]
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)
        left = ((start - min_start).total_seconds() / total_seconds) * 100
        width = max(((end - start).total_seconds() / total_seconds) * 100, 0.5)
        rows.extend(
            [
                '<div class="chart-row" role="listitem">',
                (
                    '<div class="chart-label">'
                    f"<strong>{escape_text(bar['id'])}</strong>"
                    f"<span>{escape_text(bar['status'])}</span>"
                    "</div>"
                ),
                '<div class="chart-track">',
                (
                    f'<div class="bar {escape_attr(bar["kind"])}" '
                    f'style="left:{left:.4f}%;width:{width:.4f}%">'
                    f"{escape_text(bar['title'])}"
                    "</div>"
                ),
                "</div>",
                "</div>",
            ]
        )
    rows.extend(["</div>", "</section>"])
    return "\n".join(rows)


def render_warning_section(warnings: list[dict[str, object]]) -> str:
    lines = [
        '<section class="warnings">',
        "<h2>Warnings</h2>",
    ]
    if not warnings:
        lines.append('<p class="empty">No warnings.</p>')
    else:
        lines.append("<ul>")
        for warning in warnings:
            task_id = warning.get("task_id")
            task_text = f" task={task_id}" if task_id else ""
            message = warning.get("message", "")
            lines.append(
                "<li>"
                f"<strong>{escape_text(warning.get('code'))}</strong>"
                f"{escape_text(task_text)}"
                f"<span>{escape_text(message)}</span>"
                "</li>"
            )
        lines.append("</ul>")
    lines.append("</section>")
    return "\n".join(lines)


def render_task_table(tasks: list[dict[str, object]]) -> str:
    lines = [
        '<section class="tasks">',
        "<h2>Tasks</h2>",
        "<table>",
        "<thead>",
        "<tr>",
        "<th>ID</th>",
        "<th>Status</th>",
        "<th>Priority</th>",
        "<th>Span</th>",
        "<th>Duration</th>",
        "<th>Dependencies</th>",
        "</tr>",
        "</thead>",
        "<tbody>",
    ]
    for task in tasks:
        span = task_span(task)
        lines.extend(
            [
                "<tr>",
                f"<td>{escape_text(task.get('id'))}</td>",
                f"<td>{escape_text(task.get('status'))}</td>",
                f"<td>{escape_text(task.get('priority'))}</td>",
                f"<td>{escape_text(span['label'])}</td>",
                f"<td>{escape_text(span['duration'])}</td>",
                f"<td>{escape_text(', '.join(string_list(task.get('dependencies'))))}</td>",
                "</tr>",
            ]
        )
    lines.extend(["</tbody>", "</table>", "</section>"])
    return "\n".join(lines)


def timeline_tasks(timeline: dict[str, object]) -> list[dict[str, object]]:
    tasks = timeline.get("tasks", [])
    if not isinstance(tasks, list):
        return []
    return [task for task in tasks if isinstance(task, dict)]


def timeline_warnings(timeline: dict[str, object]) -> list[dict[str, object]]:
    warnings = timeline.get("warnings", [])
    if not isinstance(warnings, list):
        return []
    return [warning for warning in warnings if isinstance(warning, dict)]


def gantt_bars(tasks: list[dict[str, object]]) -> list[dict[str, object]]:
    bars: list[dict[str, object]] = []
    for task in tasks:
        span = task_span(task)
        if span["start_dt"] is None or span["end_dt"] is None:
            continue
        bars.append(
            {
                "id": string_value(task.get("id")),
                "title": string_value(task.get("title"))
                or string_value(task.get("id")),
                "status": string_value(task.get("status")),
                "kind": span["kind"],
                "start": span["start_dt"],
                "end": span["end_dt"],
            }
        )
    return bars


def task_span(task: dict[str, object]) -> dict[str, object]:
    actual = task.get("actual")
    if isinstance(actual, dict):
        start = string_value(actual.get("start"))
        end = string_value(actual.get("end"))
        duration = actual.get("duration_minutes")
        return {
            "kind": "actual",
            "start_dt": parse_datetime(start),
            "end_dt": parse_datetime(end),
            "label": f"actual {start} -> {end}",
            "duration": duration_label(duration),
        }
    projected = task.get("projected")
    if isinstance(projected, dict) and projected.get("blocked") is not True:
        start = string_value(projected.get("start"))
        end = string_value(projected.get("end"))
        duration = projected.get("duration_minutes")
        return {
            "kind": "projected",
            "start_dt": parse_datetime(start),
            "end_dt": parse_datetime(end),
            "label": f"projected {start} -> {end}",
            "duration": duration_label(duration),
        }
    if isinstance(projected, dict) and projected.get("blocked") is True:
        blockers = ", ".join(string_list(projected.get("blockers")))
        return {
            "kind": "blocked",
            "start_dt": None,
            "end_dt": None,
            "label": f"blocked: {blockers}",
            "duration": "",
        }
    return {
        "kind": "unscheduled",
        "start_dt": None,
        "end_dt": None,
        "label": "unscheduled",
        "duration": "",
    }


def gantt_summary(tasks: list[dict[str, object]]) -> dict[str, int]:
    summary = {"actual": 0, "projected": 0, "blocked": 0}
    for task in tasks:
        span = task_span(task)
        kind = str(span["kind"])
        if kind in summary:
            summary[kind] += 1
    return summary


def chart_bounds(
    bars: list[dict[str, object]],
) -> tuple[datetime | None, datetime | None]:
    starts = [bar["start"] for bar in bars if isinstance(bar.get("start"), datetime)]
    ends = [bar["end"] for bar in bars if isinstance(bar.get("end"), datetime)]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def parse_gantt_metadata(text: str) -> dict[str, object] | None:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    if not first_line.startswith(GANTT_MARKER_PREFIX):
        return None
    if not first_line.endswith(GANTT_MARKER_SUFFIX):
        return None
    payload = first_line[len(GANTT_MARKER_PREFIX) : -len(GANTT_MARKER_SUFFIX)]
    try:
        metadata = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(metadata, dict):
        return None
    return metadata


def repo_relative_output_path(repo: Path, path: Path, name: str) -> Path:
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be a repo-relative path")
    return repo / path


def config_with_cli_artifact_outputs(
    config: VibeConfig,
    paths: PlanningArtifactPaths,
) -> VibeConfig:
    replacements: dict[str, str] = {}
    if paths.timeline_json_source == "cli":
        replacements["timeline_json"] = repo_relative_string(
            config.repo,
            paths.timeline_json,
        )
    if paths.gantt_html_source == "cli":
        replacements["gantt_html"] = repo_relative_string(
            config.repo,
            paths.gantt_html,
        )
    if not replacements:
        return config
    outputs = dataclasses.replace(
        config.planning_analytics.outputs,
        **replacements,
        explicit_keys=config.planning_analytics.outputs.explicit_keys
        | frozenset(replacements),
    )
    planning_analytics = dataclasses.replace(
        config.planning_analytics,
        outputs=outputs,
    )
    return dataclasses.replace(config, planning_analytics=planning_analytics)


def repo_relative_string(repo: Path, path: Path) -> str:
    return path.resolve().relative_to(repo).as_posix()


def planning_artifacts_command(
    config: VibeConfig,
    *,
    output: Path | None,
    html_output: Path | None,
) -> str:
    parts = [
        "vibe-loop",
        "planning",
        "artifacts",
        "--repo",
        str(config.repo),
    ]
    if output is not None:
        parts.extend(["--output", output.as_posix()])
    if html_output is not None:
        parts.extend(["--html-output", html_output.as_posix()])
    return " ".join(shlex.quote(part) for part in parts)


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def duration_label(value: object) -> str:
    if value is None:
        return ""
    return f"{value} min"


def string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [string_value(item) for item in value]


def string_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def escape_text(value: object) -> str:
    return html.escape(string_value(value), quote=False)


def escape_attr(value: object) -> str:
    return html.escape(string_value(value), quote=True)


STATIC_GANTT_CSS = """
:root {
  color-scheme: light;
  --bg: #f8faf7;
  --ink: #18211d;
  --muted: #5d6a63;
  --line: #d8ded8;
  --panel: #ffffff;
  --actual: #2f6f5e;
  --projected: #7b5e2a;
}
* {
  box-sizing: border-box;
}
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
main {
  max-width: 1180px;
  margin: 0 auto;
  padding: 28px 20px 40px;
}
h1, h2 {
  margin: 0;
  line-height: 1.15;
}
h1 {
  font-size: 28px;
}
h2 {
  font-size: 18px;
}
header {
  display: grid;
  gap: 18px;
  margin-bottom: 24px;
}
dl {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px;
  margin: 0;
}
dl div {
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 8px;
  padding: 10px 12px;
}
dt {
  color: var(--muted);
  font-size: 12px;
  margin-bottom: 4px;
}
dd {
  margin: 0;
  font-weight: 650;
}
section {
  margin-top: 24px;
}
.chart {
  margin-top: 12px;
  border: 1px solid var(--line);
  background: var(--panel);
  border-radius: 8px;
  overflow: hidden;
}
.chart-row {
  display: grid;
  grid-template-columns: minmax(150px, 230px) 1fr;
  min-height: 44px;
  border-top: 1px solid var(--line);
}
.chart-row:first-child {
  border-top: 0;
}
.chart-label {
  display: grid;
  gap: 2px;
  align-content: center;
  padding: 8px 10px;
  border-right: 1px solid var(--line);
  min-width: 0;
}
.chart-label strong,
.chart-label span,
.bar {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.chart-label span {
  color: var(--muted);
  font-size: 12px;
}
.chart-track {
  position: relative;
  min-height: 44px;
  background: linear-gradient(90deg, rgba(0,0,0,0.05) 1px, transparent 1px);
  background-size: 10% 100%;
}
.bar {
  position: absolute;
  top: 9px;
  height: 26px;
  border-radius: 4px;
  padding: 4px 8px;
  color: #fff;
  font-size: 12px;
  font-weight: 650;
}
.bar.actual {
  background: var(--actual);
}
.bar.projected {
  background: var(--projected);
}
.warnings ul {
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 8px;
}
.warnings li {
  display: grid;
  gap: 4px;
  border: 1px solid #d7c9aa;
  background: #fff9ec;
  border-radius: 8px;
  padding: 10px 12px;
}
.warnings span,
.empty {
  color: var(--muted);
}
table {
  width: 100%;
  margin-top: 12px;
  border-collapse: collapse;
  background: var(--panel);
  border: 1px solid var(--line);
}
th,
td {
  padding: 9px 10px;
  border-top: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}
th {
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}
@media (max-width: 760px) {
  main {
    padding: 20px 12px 32px;
  }
  .chart-row {
    grid-template-columns: 1fr;
  }
  .chart-label {
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .chart-track {
    min-height: 38px;
  }
  table {
    display: block;
    overflow-x: auto;
  }
}
""".strip()
