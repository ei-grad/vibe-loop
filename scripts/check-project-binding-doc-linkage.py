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
DESIGN_PATH = pathlib.Path("docs/prd/autopilot.md")
DESIGN_ID = "PRD-AUT-020"
DESIGN_ANCHOR = "prd-aut-020"
DESIGN_REFERENCE = f"{DESIGN_PATH}#{DESIGN_ANCHOR}"
ANCHOR_MARKER = f'<a id="{DESIGN_ANCHOR}"></a>'


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
        return text[anchor_offset:]
    section_end = section_start + 1 + next_heading.start()
    return text[anchor_offset:section_end]


def markdown_link_targets(repository: pathlib.Path, text: str) -> list[str]:
    checker = runpy.run_path(
        str(repository / "scripts/check-md-links.py"),
        run_name="code_doc_linkage_markdown_checker",
    )
    link_pattern = checker["LINK_RE"]
    return [match.group(1).strip().split()[0] for match in link_pattern.finditer(text)]


def section_links_to_module(
    repository: pathlib.Path,
    section: str,
) -> bool:
    design_file = repository / DESIGN_PATH
    module_file = (repository / MODULE_PATH).resolve()
    for raw_target in markdown_link_targets(repository, section):
        target = raw_target.split("#", 1)[0]
        if not target or target.startswith(("http://", "https://", "mailto:", "<")):
            continue
        if (design_file.parent / target).resolve() == module_file:
            return True
    return False


def check_linkage(repository: pathlib.Path) -> list[str]:
    errors: list[str] = []
    module_file = repository / MODULE_PATH
    design_file = repository / DESIGN_PATH

    if not module_file.is_file():
        errors.append(f"missing module: {MODULE_PATH}")
    if not design_file.is_file():
        errors.append(f"missing design record: {DESIGN_PATH}")
    if errors:
        return errors

    try:
        module = ast.parse(
            module_file.read_text(encoding="utf-8"), filename=MODULE_PATH
        )
    except (OSError, SyntaxError) as exc:
        errors.append(f"cannot read module docstring from {MODULE_PATH}: {exc}")
    else:
        docstring = ast.get_docstring(module, clean=False)
        if docstring is None or DESIGN_REFERENCE not in docstring:
            errors.append(
                f"{MODULE_PATH} module docstring does not reference {DESIGN_REFERENCE}"
            )

    design_text = design_file.read_text(encoding="utf-8")
    section = project_binding_section(design_text)
    if section is None:
        errors.append(
            f"{DESIGN_PATH} is missing {ANCHOR_MARKER} followed by {DESIGN_ID}"
        )
    elif not section_links_to_module(repository, section):
        errors.append(f"{DESIGN_ID} does not link to {MODULE_PATH}")

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
