#!/usr/bin/env python3
# Vendored from capos:scripts/check-md-links.py at 9a26d64af6f86e88db43da39887a79103f2393ae.
"""Validate relative Markdown links and configured documentation reachability."""

import argparse
import collections
import pathlib
import re
import subprocess
import sys

import tomllib

DEFAULT_CONFIG = pathlib.Path("markdown-links.toml")
LINK_RE = re.compile(r"\]\(([^)]+)\)")
TASK_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ConfigError(ValueError):
    pass


def git_lines(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.splitlines()


def select_markdown(paths: list[str]) -> list[pathlib.Path]:
    repository = pathlib.Path(git_lines("rev-parse", "--show-toplevel")[0]).resolve()
    selected = set()
    for value in paths:
        path = pathlib.Path(value)
        if not path.exists() or path.suffix != ".md":
            continue
        try:
            relative = path.resolve().relative_to(repository)
        except ValueError as exc:
            raise ConfigError(
                f"Markdown path is outside the repository: {path}"
            ) from exc
        if relative.parts[:1] != ("vendor",):
            selected.add(relative)
    return sorted(selected)


def all_markdown() -> list[pathlib.Path]:
    return select_markdown(
        git_lines("ls-files", "*.md")
        + git_lines("ls-files", "--others", "--exclude-standard", "*.md")
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate Markdown links and documentation reachability."
    )
    parser.add_argument(
        "--staged",
        action="store_true",
        help="report link failures and orphans only for staged Markdown files",
    )
    parser.add_argument(
        "--config",
        type=pathlib.Path,
        default=DEFAULT_CONFIG,
        help=f"reachability configuration (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("paths", nargs="*", help="Markdown files to check")
    args = parser.parse_args()
    if args.staged and args.paths:
        parser.error("--staged cannot be combined with explicit paths")
    return args


def load_config(
    path: pathlib.Path,
) -> tuple[tuple[pathlib.Path, ...], tuple[pathlib.Path, ...], int]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"missing config: {path}") from exc
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"invalid config {path}: {exc}") from exc

    reachability = data.get("reachability")
    if not isinstance(reachability, dict):
        raise ConfigError(f"{path}: [reachability] table is required")
    raw_roots = reachability.get("roots")
    if (
        not isinstance(raw_roots, list)
        or not raw_roots
        or any(not isinstance(root, str) or not root for root in raw_roots)
    ):
        raise ConfigError(f"{path}: reachability.roots must be non-empty strings")
    max_hops = reachability.get("max_hops")
    if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 1:
        raise ConfigError(f"{path}: reachability.max_hops must be a positive integer")

    raw_exclude = reachability.get("exclude", [])
    if not isinstance(raw_exclude, list) or any(
        not isinstance(excluded, str) or not excluded for excluded in raw_exclude
    ):
        raise ConfigError(f"{path}: reachability.exclude must contain strings")

    roots = tuple(pathlib.Path(root) for root in raw_roots)
    excluded = tuple(pathlib.Path(value) for value in raw_exclude)
    for candidate in roots + excluded:
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError(
                f"{path}: configured path must be repository-relative: {candidate}"
            )
        if candidate.suffix != ".md":
            raise ConfigError(
                f"{path}: configured path must be a Markdown file: {candidate}"
            )
    for root in roots:
        if not root.exists():
            raise ConfigError(f"{path}: root does not exist: {root}")
        if root in excluded:
            raise ConfigError(f"{path}: root cannot be excluded: {root}")
    return roots, excluded, max_hops


def markdown_targets(path: pathlib.Path) -> list[tuple[str, str]]:
    targets = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for match in LINK_RE.finditer(text):
        raw = match.group(1).strip().split()[0]
        target = raw.split("#")[0]
        targets.append((raw, target))
    return targets


def resolve_markdown_target(
    source: pathlib.Path,
    raw: str,
    target: str,
    excluded: set[pathlib.Path],
) -> pathlib.Path | None:
    if (
        not target
        or target.startswith(("http://", "https://", "mailto:", "<"))
        or not target.endswith(".md")
        or raw.startswith("task:")
    ):
        return None
    resolved = (source.parent / target).resolve()
    return None if resolved in excluded else resolved


def broken_links(
    files: list[pathlib.Path],
    excluded: set[pathlib.Path],
) -> list[tuple[pathlib.Path, str]]:
    broken = []
    for source in files:
        for raw, target in markdown_targets(source):
            if raw.startswith("task:"):
                if not TASK_RE.fullmatch(raw.removeprefix("task:")):
                    broken.append((source, raw))
                continue
            resolved = resolve_markdown_target(source, raw, target, excluded)
            if resolved is not None and not resolved.exists():
                broken.append((source, target))
    return broken


def link_graph(
    files: list[pathlib.Path],
    excluded: set[pathlib.Path],
) -> dict[pathlib.Path, set[pathlib.Path]]:
    canonical = {path.resolve(): path for path in files}
    graph = {path: set() for path in files}
    for source in files:
        for raw, target in markdown_targets(source):
            resolved = resolve_markdown_target(source, raw, target, excluded)
            if resolved in canonical:
                graph[source].add(canonical[resolved])
    return graph


def reachable_documents(
    graph: dict[pathlib.Path, set[pathlib.Path]],
    roots: tuple[pathlib.Path, ...],
    max_hops: int,
) -> set[pathlib.Path]:
    reached = set(roots)
    frontier = set(roots)
    for _ in range(max_hops):
        frontier = {
            target
            for source in frontier
            for target in graph.get(source, ())
            if target not in reached
        }
        reached.update(frontier)
    return reached


def is_documentation(
    path: pathlib.Path,
    excluded: set[pathlib.Path],
) -> bool:
    return path.resolve() not in excluded and path.parts[:1] == ("docs",)


def nearest_reachable(
    orphan: pathlib.Path,
    graph: dict[pathlib.Path, set[pathlib.Path]],
    reachable: set[pathlib.Path],
) -> pathlib.Path | None:
    neighbours = {path: set(targets) for path, targets in graph.items()}
    for source, targets in graph.items():
        for target in targets:
            neighbours.setdefault(target, set()).add(source)

    queue = collections.deque([orphan])
    visited = {orphan}
    while queue:
        source = queue.popleft()
        for target in sorted(neighbours.get(source, ())):
            if target in visited:
                continue
            if target in reachable:
                return target
            visited.add(target)
            queue.append(target)
    return None


def main() -> int:
    args = parse_args()
    if args.staged:
        checked = select_markdown(
            git_lines(
                "diff",
                "--cached",
                "--name-only",
                "--diff-filter=ACMR",
                "--",
                "*.md",
            )
        )
    elif args.paths:
        checked = select_markdown(args.paths)
    else:
        checked = all_markdown()

    if not checked:
        print("checked 0 md files; 0 broken .md link(s); 0 orphan doc(s)")
        return 0

    roots, excluded_paths, max_hops = load_config(args.config)
    excluded = {path.resolve() for path in excluded_paths}
    all_files = all_markdown()
    graph = link_graph(all_files, excluded)
    reachable = reachable_documents(graph, roots, max_hops)

    broken = broken_links(checked, excluded)
    orphan_candidates = checked if args.staged or args.paths else all_files
    orphans = sorted(
        path
        for path in orphan_candidates
        if is_documentation(path, excluded) and path not in reachable
    )

    for source, target in broken:
        print(f"broken link: {source} -> {target}")
    for orphan in orphans:
        message = (
            f"orphan doc: {orphan} is not reachable from configured roots "
            f"within {max_hops} link hop(s)"
        )
        neighbour = nearest_reachable(orphan, graph, reachable)
        if neighbour is not None:
            message += f"; nearest reachable neighbour: {neighbour}"
        print(message)
    print(
        f"checked {len(checked)} md files; {len(broken)} broken .md link(s); "
        f"{len(orphans)} orphan doc(s)"
    )
    return 1 if broken or orphans else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ConfigError, OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
