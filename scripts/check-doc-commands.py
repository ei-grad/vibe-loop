#!/usr/bin/env python3
"""Check documented vibe-loop command paths against the CLI parser."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
INLINE_COMMAND_RE = re.compile(r"\bvibe-loop(?:\s+([^`\n]+))?")
COMMAND_LINE_RE = re.compile(r"^\s*(?:\$\s+)?(?:uv\s+run\s+)?vibe-loop(?:\s+(.+))?$")
FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
COMMAND_TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")
SHELL_FENCE_LANGUAGES = {"", "bash", "console", "sh", "shell", "terminal", "zsh"}
ALLOW_REMOVED_RE = re.compile(
    r"<!--\s*doc-command:\s*allow-removed\s+"
    r"([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)*)\s*-->"
)


@dataclass(frozen=True)
class CommandReference:
    path: Path
    line: int
    tokens: tuple[str, ...]

    @property
    def command(self) -> str:
        return " ".join(self.tokens)


@dataclass(frozen=True)
class Fence:
    marker: str
    length: int
    scans_commands: bool


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
    fence: Fence | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        fence_match = FENCE_RE.match(line)
        if fence is None and fence_match is not None:
            marker = fence_match.group(2)
            info = fence_match.group(3).strip().split(maxsplit=1)
            language = info[0].lower() if info else ""
            fence = Fence(
                marker=marker[0],
                length=len(marker),
                scans_commands=language in SHELL_FENCE_LANGUAGES,
            )
            continue

        if fence is not None:
            if closes_fence(line, fence):
                fence = None
                continue
            if fence.scans_commands:
                command_line = COMMAND_LINE_RE.match(line)
                if command_line is not None:
                    tokens = command_tokens(command_line.group(1) or "")
                    if tokens:
                        references.append(CommandReference(path, line_number, tokens))
            continue

        for inline_code in INLINE_CODE_RE.finditer(line):
            for command in INLINE_COMMAND_RE.finditer(inline_code.group(1)):
                tokens = command_tokens(command.group(1) or "")
                if tokens:
                    references.append(CommandReference(path, line_number, tokens))
    return references


def closes_fence(line: str, fence: Fence) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3 or not stripped.startswith(fence.marker):
        return False
    marker_length = len(stripped) - len(stripped.lstrip(fence.marker))
    return marker_length >= fence.length and not stripped[marker_length:].strip()


def command_tokens(remainder: str) -> tuple[str, ...]:
    tokens: list[str] = []
    for raw_token in remainder.split():
        token = raw_token.rstrip(".,:;")
        if not COMMAND_TOKEN_RE.fullmatch(token):
            break
        tokens.append(token)
    return tuple(tokens)


def command_paths(
    parser: argparse.ArgumentParser,
    prefix: tuple[str, ...] = (),
) -> tuple[set[tuple[str, ...]], set[tuple[str, ...]]]:
    paths: set[tuple[str, ...]] = set()
    parents: set[tuple[str, ...]] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            if prefix:
                parents.add(prefix)
            for command, child_parser in action.choices.items():
                path = (*prefix, command)
                paths.add(path)
                child_paths, child_parents = command_paths(child_parser, path)
                paths.update(child_paths)
                parents.update(child_parents)
    return paths, parents


def allowed_removed_paths(text: str) -> set[tuple[str, ...]]:
    return {tuple(match.group(1).split()) for match in ALLOW_REMOVED_RE.finditer(text)}


def unresolved_path(
    reference: CommandReference,
    paths: set[tuple[str, ...]],
    parents: set[tuple[str, ...]],
) -> tuple[str, ...] | None:
    resolved: tuple[str, ...] = ()
    for token in reference.tokens:
        if resolved and resolved not in parents:
            break
        candidate = (*resolved, token)
        if candidate not in paths:
            return candidate
        resolved = candidate
    return None


def path_is_allowed(
    unresolved: tuple[str, ...],
    allowed: set[tuple[str, ...]],
) -> bool:
    return any(unresolved[: len(path)] == path for path in allowed)


def unresolved_references(
    documents: dict[Path, str],
    paths: set[tuple[str, ...]],
    parents: set[tuple[str, ...]],
) -> list[CommandReference]:
    unresolved: list[CommandReference] = []
    for path, text in documents.items():
        allowed = allowed_removed_paths(text)
        for reference in documented_commands(path, text):
            missing_path = unresolved_path(reference, paths, parents)
            if missing_path is not None and not path_is_allowed(missing_path, allowed):
                unresolved.append(
                    CommandReference(reference.path, reference.line, missing_path)
                )
    return unresolved


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repository = repository_root()
    sys.path.insert(0, str(repository / "src"))
    from vibe_loop.cli import build_parser

    paths, parents = command_paths(build_parser())
    unresolved = unresolved_references(
        tracked_markdown(repository, staged=args.staged),
        paths,
        parents,
    )
    for reference in unresolved:
        print(
            f"{reference.path.as_posix()}:{reference.line}: "
            f"unknown documented command 'vibe-loop {reference.command}'"
        )
    if unresolved:
        return 1
    print("documented command paths: ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"documented command paths: error: {exc}", file=sys.stderr)
        sys.exit(2)
