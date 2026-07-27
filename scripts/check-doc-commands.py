#!/usr/bin/env python3
"""Check that documented vibe-loop commands resolve to top-level subcommands."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
COMMAND_RE = re.compile(r"\bvibe-loop\s+([a-z][a-z0-9-]*)\b")
COMMAND_LINE_RE = re.compile(r"^\s*(?:\$\s+)?vibe-loop\s+([a-z][a-z0-9-]*)\b")
FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")


@dataclass(frozen=True)
class CommandReference:
    path: Path
    line: int
    command: str


def repository_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def tracked_markdown(repository: Path, *, staged: bool) -> dict[Path, str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--", "*.md"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    documents: dict[Path, str] = {}
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative_path = Path(raw_path.decode("utf-8"))
        if staged:
            content = subprocess.run(
                ["git", "show", f":{relative_path.as_posix()}"],
                cwd=repository,
                check=True,
                capture_output=True,
            ).stdout.decode("utf-8")
        else:
            path = repository / relative_path
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
        documents[relative_path] = content
    return documents


def documented_commands(path: Path, text: str) -> list[CommandReference]:
    references: list[CommandReference] = []
    fence_marker: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        fence = FENCE_RE.match(line)
        if fence is not None:
            marker = fence.group(1)
            if fence_marker is None:
                fence_marker = marker[0]
            elif marker[0] == fence_marker:
                fence_marker = None
            continue

        if fence_marker is not None:
            command_line = COMMAND_LINE_RE.match(line)
            if command_line is not None:
                references.append(
                    CommandReference(path, line_number, command_line.group(1))
                )
            continue

        for inline_code in INLINE_CODE_RE.finditer(line):
            for command in COMMAND_RE.finditer(inline_code.group(1)):
                references.append(CommandReference(path, line_number, command.group(1)))
    return references


def top_level_commands(parser: argparse.ArgumentParser) -> set[str]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    raise RuntimeError("vibe-loop parser has no top-level subcommands")


def unresolved_references(
    documents: dict[Path, str],
    commands: set[str],
) -> list[CommandReference]:
    return [
        reference
        for path, text in documents.items()
        for reference in documented_commands(path, text)
        if reference.command not in commands
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = repository_root()
    sys.path.insert(0, str(repository / "src"))
    from vibe_loop.cli import build_parser

    commands = top_level_commands(build_parser())
    unresolved = unresolved_references(
        tracked_markdown(repository, staged=args.staged),
        commands,
    )
    for reference in unresolved:
        print(
            f"{reference.path.as_posix()}:{reference.line}: "
            f"unknown documented command 'vibe-loop {reference.command}'"
        )
    if unresolved:
        return 1
    print("documented command surface: ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"documented command surface: error: {exc}", file=sys.stderr)
        sys.exit(2)
