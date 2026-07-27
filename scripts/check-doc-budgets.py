#!/usr/bin/env python3
# Vendored from capos:scripts/check-doc-budgets.py at 6c8083fcec22e4ac9a6a338022000ff4a23dea79.
"""Enforce ratcheting Markdown size and structural budgets."""

from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

HEADING_RE = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+|$)")
FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
LIST_RE = re.compile(r"^ {0,3}(?:[-+*]|\d+[.)])[ \t]+")
BLOCKQUOTE_RE = re.compile(r"^ {0,3}>")
BOLD_LEAD_RE = re.compile(r"^\s*\*\*(?=\S)(.+?)\*\*(?=[:\s]|$)")
TABLE_DELIMITER_CELL_RE = re.compile(r"^:?-{3,}:?$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Budget:
    paths: tuple[str, ...]
    kind: str
    target: int
    baseline: int
    deadline: dt.date

    @property
    def label(self) -> str:
        return " + ".join(self.paths) if self.kind == "aggregate" else self.paths[0]


@dataclass(frozen=True)
class ProseRun:
    start_line: int
    end_line: int
    byte_count: int
    heading_depth: int
    bold_leads: tuple[int, ...]


@dataclass
class CheckResult:
    errors: list[str]
    warnings: list[str]


def run_git(repo: Path, *args: str, text: bool = False) -> bytes | str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return completed.stdout


class RepositoryView:
    def __init__(self, repo: Path, staged: bool) -> None:
        self.repo = repo
        self.staged = staged

    def paths(self) -> list[str]:
        output = run_git(self.repo, "ls-files", "-z")
        assert isinstance(output, bytes)
        paths = [
            path.decode("utf-8")
            for path in output.split(b"\0")
            if path and not path.endswith(b"/")
        ]
        if not self.staged:
            paths = [path for path in paths if (self.repo / path).is_file()]
        return sorted(paths)

    def changed_paths(self) -> set[str]:
        if not self.staged:
            raise ConfigError("changed_paths is available only for staged views")
        output = run_git(
            self.repo,
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMRDTUXB",
            "-z",
        )
        assert isinstance(output, bytes)
        return {
            path.decode("utf-8")
            for path in output.split(b"\0")
            if path and not path.endswith(b"/")
        }

    def read(self, path: str) -> bytes:
        if self.staged:
            try:
                output = run_git(self.repo, "show", f":{path}")
            except subprocess.CalledProcessError as exc:
                raise ConfigError(
                    f"staged file is missing from the index: {path}"
                ) from exc
            assert isinstance(output, bytes)
            return output
        try:
            return (self.repo / path).read_bytes()
        except FileNotFoundError as exc:
            raise ConfigError(
                f"tracked file is missing from the worktree: {path}"
            ) from exc


def normalize_config_path(repo: Path, config: Path) -> str:
    absolute = config if config.is_absolute() else repo / config
    try:
        return absolute.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError as exc:
        raise ConfigError("config must be inside the repository") from exc


def parse_config(raw: bytes) -> tuple[list[Budget], int, int, dict[str, int]]:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc

    defaults = data.get("defaults")
    if not isinstance(defaults, dict) or defaults.get("estimate") != "bytes/4":
        raise ConfigError('[defaults].estimate must be "bytes/4"')

    rows = data.get("budget")
    if not isinstance(rows, list) or not rows:
        raise ConfigError("at least one [[budget]] entry is required")

    budgets: list[Budget] = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise ConfigError(f"budget {index} must be a table")
        paths = row.get("paths")
        if (
            not isinstance(paths, list)
            or not paths
            or any(not isinstance(path, str) or not path for path in paths)
        ):
            raise ConfigError(f"budget {index} paths must be non-empty strings")
        kind = row.get("kind", "single")
        if kind not in {"single", "aggregate"}:
            raise ConfigError(f"budget {index} kind must be single or aggregate")
        if kind == "single" and len(paths) != 1:
            raise ConfigError(f"budget {index} single kind requires one path pattern")
        target = row.get("target")
        baseline = row.get("baseline")
        if (
            not isinstance(target, int)
            or isinstance(target, bool)
            or target < 0
            or not isinstance(baseline, int)
            or isinstance(baseline, bool)
            or baseline < target
        ):
            raise ConfigError(
                f"budget {index} requires integer 0 <= target <= baseline"
            )
        try:
            deadline = dt.date.fromisoformat(row["deadline"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"budget {index} has an invalid deadline") from exc
        budgets.append(Budget(tuple(paths), kind, target, baseline, deadline))

    structure = data.get("structure")
    if not isinstance(structure, dict):
        raise ConfigError("[structure] is required")
    warn = structure.get("warn")
    hard = structure.get("hard")
    if (
        not isinstance(warn, int)
        or isinstance(warn, bool)
        or warn < 1
        or not isinstance(hard, int)
        or isinstance(hard, bool)
        or hard <= warn
    ):
        raise ConfigError("structure requires integer 0 < warn < hard")
    baselines = structure.get("baselines", {})
    if not isinstance(baselines, dict) or any(
        not isinstance(path, str)
        or not isinstance(value, int)
        or isinstance(value, bool)
        or value <= hard
        for path, value in baselines.items()
    ):
        raise ConfigError("structure.baselines values must exceed structure.hard")
    return budgets, warn, hard, dict(baselines)


def matches(path: str, pattern: str) -> bool:
    if "/" not in pattern:
        return "/" not in path and fnmatch.fnmatchcase(path, pattern)
    return PurePosixPath(path).match(pattern)


def budget_current(
    budget: Budget, markdown_paths: list[str], view: RepositoryView
) -> int:
    matched_by_pattern = {
        pattern: [path for path in markdown_paths if matches(path, pattern)]
        for pattern in budget.paths
    }
    unmatched = [
        pattern for pattern, matched in matched_by_pattern.items() if not matched
    ]
    if unmatched:
        names = ", ".join(repr(pattern) for pattern in unmatched)
        raise ConfigError(
            f"budget {budget.label!r} has unmatched required path pattern(s): {names}"
        )
    matched = sorted(
        {path for paths in matched_by_pattern.values() for path in paths}
    )
    if budget.kind == "single" and len(matched) != 1:
        raise ConfigError(
            f"single budget {budget.label!r} matches {len(matched)} tracked files"
        )
    byte_count = sum(len(view.read(path)) for path in matched)
    return (byte_count + 3) // 4


def unescaped_pipe_count(line: str) -> int:
    count = 0
    escaped = False
    for character in line:
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "|":
            count += 1
    return count


def is_table_delimiter(line: str) -> bool:
    stripped = line.strip()
    if unescaped_pipe_count(stripped) == 0:
        return False
    cells = re.split(r"(?<!\\)\|", stripped)
    if cells and not cells[0]:
        cells.pop(0)
    if cells and not cells[-1]:
        cells.pop()
    return bool(cells) and all(
        TABLE_DELIMITER_CELL_RE.fullmatch(cell.strip()) for cell in cells
    )


def table_line_numbers(lines: list[str], front_matter_end: int) -> set[int]:
    table_lines: set[int] = set()
    in_fence = False
    fence_char = ""
    fence_width = 0
    for index, line in enumerate(lines):
        line_number = index + 1
        if line_number <= front_matter_end:
            continue
        logical = line.rstrip("\r\n")
        fence = FENCE_RE.match(logical)
        if in_fence:
            stripped = logical.lstrip(" ")
            if (
                stripped.startswith(fence_char * fence_width)
                and stripped.rstrip(fence_char).strip() == ""
            ):
                in_fence = False
            continue
        if fence:
            marker = fence.group(1)
            in_fence = True
            fence_char = marker[0]
            fence_width = len(marker)
            continue
        if not is_table_delimiter(logical) or index == 0:
            continue
        header = lines[index - 1].rstrip("\r\n")
        if unescaped_pipe_count(header) == 0:
            continue
        table_lines.update({line_number - 1, line_number})
        following = index + 1
        while following < len(lines):
            row = lines[following].rstrip("\r\n")
            if not row.strip() or unescaped_pipe_count(row) == 0:
                break
            table_lines.add(following + 1)
            following += 1
    return table_lines


def markdown_runs(raw: bytes) -> list[ProseRun]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("Markdown files must be UTF-8") from exc
    lines = text.splitlines(keepends=True)
    runs: list[ProseRun] = []
    front_matter_end = 0
    if lines and lines[0].rstrip("\r\n") == "---":
        for index, line in enumerate(lines[1:], 2):
            if line.rstrip("\r\n") == "---":
                front_matter_end = index
                break
    tables = table_line_numbers(lines, front_matter_end)
    start_line = front_matter_end + 1
    byte_count = 0
    heading_depth = 0
    bold_leads: list[int] = []
    in_fence = False
    fence_char = ""
    fence_width = 0
    in_list = False
    paragraph_start = True

    def finish(end_line: int) -> None:
        nonlocal start_line, byte_count, bold_leads
        if byte_count:
            runs.append(
                ProseRun(
                    start_line,
                    max(start_line, end_line),
                    byte_count,
                    heading_depth,
                    tuple(bold_leads),
                )
            )
        byte_count = 0
        bold_leads = []

    for line_number, line in enumerate(lines, 1):
        if line_number <= front_matter_end:
            continue
        logical = line.rstrip("\r\n")
        fence = FENCE_RE.match(logical)
        if in_fence:
            stripped = logical.lstrip(" ")
            if (
                stripped.startswith(fence_char * fence_width)
                and stripped.rstrip(fence_char).strip() == ""
            ):
                in_fence = False
            paragraph_start = True
            continue
        if fence:
            marker = fence.group(1)
            in_fence = True
            fence_char = marker[0]
            fence_width = len(marker)
            paragraph_start = True
            continue

        heading = HEADING_RE.match(logical)
        if heading:
            finish(line_number - 1)
            heading_depth = len(heading.group(1))
            start_line = line_number + 1
            in_list = False
            paragraph_start = True
            continue

        stripped = logical.strip()
        is_list = bool(LIST_RE.match(logical))
        is_indented_continuation = in_list and (
            logical.startswith("\t") or len(logical) - len(logical.lstrip(" ")) >= 2
        )
        is_table = line_number in tables
        is_quote = bool(BLOCKQUOTE_RE.match(logical))
        if is_list:
            in_list = True
        elif stripped and not is_indented_continuation:
            in_list = False

        if is_list or is_indented_continuation or is_table or is_quote:
            paragraph_start = True
            continue

        if stripped:
            if paragraph_start and BOLD_LEAD_RE.match(logical):
                bold_leads.append(line_number)
            paragraph_start = False
        else:
            paragraph_start = True
        byte_count += len(line.encode("utf-8"))

    finish(len(lines))
    return runs


def check(
    view: RepositoryView,
    config_path: str,
    today: dt.date,
    changed_paths: set[str] | None = None,
) -> tuple[
    CheckResult,
    list[int],
    dict[str, int],
    tuple[list[Budget], int, int, dict[str, int]],
]:
    parsed = parse_config(view.read(config_path))
    budgets, warn_limit, hard_limit, structural_baselines = parsed
    all_paths = view.paths()
    markdown_paths = [
        path for path in all_paths if path.lower().endswith(".md") and path != "LICENSE"
    ]
    errors: list[str] = []
    warnings: list[str] = []
    currents: list[int] = []
    config_changed = changed_paths is not None and config_path in changed_paths

    for budget in budgets:
        if (
            changed_paths is not None
            and not config_changed
            and not any(
                matches(path, pattern)
                for path in changed_paths
                for pattern in budget.paths
            )
        ):
            currents.append(budget.baseline)
            continue
        current = budget_current(budget, markdown_paths, view)
        currents.append(current)
        hard_ceiling = budget.target if today > budget.deadline else budget.baseline
        if current <= budget.target:
            continue
        if current > hard_ceiling:
            errors.append(
                f"{budget.label}: {current} est. tokens exceeds "
                f"{hard_ceiling} hard limit by {current - hard_ceiling}"
            )
        else:
            warnings.append(
                f"{budget.label}: {current} est. tokens is "
                f"{current - budget.target} over target {budget.target}; "
                f"deadline {budget.deadline.isoformat()}"
            )

    structural_currents: dict[str, int] = {}
    structural_paths = (
        markdown_paths
        if changed_paths is None or config_changed
        else [path for path in markdown_paths if path in changed_paths]
    )
    for path in structural_paths:
        runs = markdown_runs(view.read(path))
        structural_currents[path] = max((run.byte_count for run in runs), default=0)
        ceiling = structural_baselines.get(path, hard_limit)
        for run in runs:
            location = f"{path}:{run.start_line}-{run.end_line}"
            if run.byte_count > ceiling:
                errors.append(
                    f"{location}: unbroken prose run is {run.byte_count} bytes; "
                    f"hard limit is {ceiling}"
                )
            elif run.byte_count > hard_limit:
                warnings.append(
                    f"{location}: grandfathered unbroken prose run is "
                    f"{run.byte_count} bytes (hard threshold {hard_limit})"
                )
            elif run.byte_count > warn_limit:
                warnings.append(
                    f"{location}: unbroken prose run is {run.byte_count} bytes "
                    f"(warning threshold {warn_limit})"
                )
            if run.byte_count > warn_limit:
                suggested = min(run.heading_depth + 1, 6)
                for line_number in run.bold_leads:
                    warnings.append(
                        f"{path}:{line_number}: paragraph-initial bold text in an "
                        f"over-threshold prose run; use a level-{suggested} heading"
                    )

    for path in structural_baselines:
        if (changed_paths is None or config_changed) and path not in markdown_paths:
            errors.append(
                f"structural baseline names an untracked Markdown file: {path}"
            )

    return CheckResult(errors, warnings), currents, structural_currents, parsed


def update_config(
    path: Path,
    raw: str,
    currents: list[int],
    structural_currents: dict[str, int],
    hard_limit: int,
) -> bool:
    budget_index = 0

    def replace_budget(section: re.Match[str]) -> str:
        nonlocal budget_index
        body = section.group(0)
        current = currents[budget_index]
        budget_index += 1
        match = re.search(r"(?m)^baseline\s*=\s*(\d+)(.*)$", body)
        if match is None:
            raise ConfigError("every [[budget]] section must contain baseline")
        old = int(match.group(1))
        target_match = re.search(r"(?m)^target\s*=\s*(\d+)(.*)$", body)
        if target_match is None:
            raise ConfigError("every [[budget]] section must contain target")
        target = int(target_match.group(1))
        new = max(target, min(old, current))
        return body[: match.start(1)] + str(new) + body[match.end(1) :]

    rewritten = re.sub(
        r"(?ms)^\[\[budget\]\]\n.*?(?=^\[\[budget\]\]\n|^\[[^\[]|\Z)",
        replace_budget,
        raw,
    )
    if budget_index != len(currents):
        raise ConfigError("could not map every parsed budget to its TOML section")

    baseline_header = re.search(
        r"(?m)^\[structure\.baselines\][ \t]*$", rewritten
    )
    if baseline_header is None:
        raise ConfigError("[structure.baselines] table is required for refresh")
    table_end = re.search(r"(?m)^\[", rewritten[baseline_header.end() :])
    end = (
        baseline_header.end() + table_end.start()
        if table_end is not None
        else len(rewritten)
    )
    entries = [
        f"{json.dumps(name)} = {value}"
        for name, value in sorted(structural_currents.items())
        if value > hard_limit
    ]
    replacement = "\n" + "\n".join(entries) + ("\n" if entries else "")
    rewritten = rewritten[: baseline_header.end()] + replacement + rewritten[end:]
    if rewritten == raw:
        return False
    path.write_text(rewritten, encoding="utf-8")
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--update-baselines", action="store_true")
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.staged and args.update_baselines:
        print(
            "ERROR: --staged and --update-baselines cannot be combined",
            file=sys.stderr,
        )
        return 2
    try:
        repo_text = run_git(Path.cwd(), "rev-parse", "--show-toplevel", text=True)
        assert isinstance(repo_text, str)
        repo = Path(repo_text.strip()).resolve()
        config_path = normalize_config_path(repo, args.config)
        view = RepositoryView(repo, args.staged)
        changed_paths = view.changed_paths() if args.staged else None
        result, currents, structural_currents, parsed = check(
            view, config_path, args.today, changed_paths
        )
        for warning in result.warnings:
            print(f"WARN: {warning}", file=sys.stderr)
        for error in result.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        if args.update_baselines:
            if result.errors:
                print("ERROR: refusing to refresh while checks fail", file=sys.stderr)
                return 1
            config_file = repo / config_path
            changed = update_config(
                config_file,
                config_file.read_text(encoding="utf-8"),
                currents,
                structural_currents,
                parsed[2],
            )
            print(
                "doc budget baselines updated"
                if changed
                else "doc budget baselines unchanged"
            )
        elif not result.errors:
            print(
                f"doc budgets: ok ({len(result.warnings)} warning"
                f"{'' if len(result.warnings) == 1 else 's'})"
            )
        return 1 if result.errors else 0
    except (ConfigError, OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
