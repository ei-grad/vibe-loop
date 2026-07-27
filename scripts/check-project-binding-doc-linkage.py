#!/usr/bin/env python3
"""Advisory check for the project_binding code/design linkage trial."""

from __future__ import annotations

import ast
import pathlib
import re
import runpy
import subprocess
import sys


MODULE_PATH = pathlib.Path("src/vibe_loop/config.py")
ENFORCEMENT_PATHS = (
    pathlib.Path("src/vibe_loop/cli.py"),
    pathlib.Path("src/vibe_loop/runner.py"),
    pathlib.Path("src/vibe_loop/autopilot.py"),
)
REQUIRED_PRD_LINKS = (MODULE_PATH, *ENFORCEMENT_PATHS)
PROJECT_BINDING_SYMBOLS = ("resolve_project_binding", "require_project_binding")
DESIGN_PATH = pathlib.Path("docs/prd/autopilot.md")
DESIGN_ID = "PRD-AUT-020"
DESIGN_ANCHOR = "prd-aut-020"
ANCHOR_MARKER = f'<a id="{DESIGN_ANCHOR}"></a>'


def display_path(path: pathlib.PurePath) -> str:
    return path.as_posix()


def anchored_reference(path: pathlib.PurePath, anchor: str) -> str:
    return f"{display_path(path)}#{anchor}"


DESIGN_REFERENCE = anchored_reference(DESIGN_PATH, DESIGN_ANCHOR)


def repository_root() -> pathlib.Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        check=True,
        text=True,
    )
    return pathlib.Path(result.stdout.strip()).resolve()


def project_binding_section(text: str) -> str | None:
    anchor_offset = text.find(ANCHOR_MARKER)
    if anchor_offset < 0:
        return None
    heading = re.search(
        rf"^## {re.escape(DESIGN_ID)}\b.*$",
        text[anchor_offset:],
        flags=re.MULTILINE,
    )
    if heading is None:
        return None
    section_start = anchor_offset + heading.start()
    next_heading = re.search(r"^## ", text[section_start + 1 :], flags=re.MULTILINE)
    if next_heading is None:
        return text[section_start:]
    section_end = section_start + 1 + next_heading.start()
    return text[section_start:section_end]


def markdown_link_targets(repository: pathlib.Path, text: str) -> list[str]:
    checker = runpy.run_path(
        str(repository / "scripts/check-md-links.py"),
        run_name="code_doc_linkage_markdown_checker",
    )
    link_pattern = checker["LINK_RE"]
    return [match.group(1).strip().split()[0] for match in link_pattern.finditer(text)]


def section_link_paths(
    repository: pathlib.Path,
    section: str,
) -> set[pathlib.Path]:
    design_file = repository / DESIGN_PATH
    linked_paths = set()
    for raw_target in markdown_link_targets(repository, section):
        target = raw_target.split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "<")):
            continue
        linked_paths.add((design_file.parent / target).resolve())
    return linked_paths


def check_linkage(repository: pathlib.Path) -> list[str]:
    errors: list[str] = []
    module_file = repository / MODULE_PATH
    design_file = repository / DESIGN_PATH

    for implementation_path in REQUIRED_PRD_LINKS:
        if not (repository / implementation_path).is_file():
            errors.append(
                f"missing implementation path: {display_path(implementation_path)}"
            )
    if not design_file.is_file():
        errors.append(f"missing design record: {display_path(DESIGN_PATH)}")
    if errors:
        return errors

    try:
        module = ast.parse(
            module_file.read_text(encoding="utf-8"),
            filename=display_path(MODULE_PATH),
        )
    except (OSError, SyntaxError) as exc:
        errors.append(
            f"cannot read module docstring from {display_path(MODULE_PATH)}: {exc}"
        )
    else:
        docstring = ast.get_docstring(module, clean=False)
        if docstring is None or DESIGN_REFERENCE not in docstring:
            errors.append(
                f"{display_path(MODULE_PATH)} module docstring does not reference "
                f"{DESIGN_REFERENCE}"
            )
        for symbol in PROJECT_BINDING_SYMBOLS:
            if docstring is None or symbol not in docstring:
                errors.append(
                    f"{display_path(MODULE_PATH)} module docstring does not name "
                    f"{symbol}"
                )
        defined_functions = {
            node.name
            for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for symbol in PROJECT_BINDING_SYMBOLS:
            if symbol not in defined_functions:
                errors.append(f"{display_path(MODULE_PATH)} does not define {symbol}")

    design_text = design_file.read_text(encoding="utf-8")
    section = project_binding_section(design_text)
    if section is None:
        errors.append(
            f"{display_path(DESIGN_PATH)} is missing {ANCHOR_MARKER} followed by "
            f"{DESIGN_ID}"
        )
    else:
        linked_paths = section_link_paths(repository, section)
        for implementation_path in REQUIRED_PRD_LINKS:
            if (repository / implementation_path).resolve() not in linked_paths:
                errors.append(
                    f"{DESIGN_ID} does not link to {display_path(implementation_path)}"
                )

    return errors


def main() -> int:
    errors = check_linkage(repository_root())
    for error in errors:
        print(f"code/doc linkage: {error}")
    if errors:
        return 1
    print("project_binding code/doc linkage: ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
