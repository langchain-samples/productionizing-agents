#!/usr/bin/env python3
"""Build notebooks/*.ipynb from notebook_src/*.py.

The sources use the jupytext "percent" format, which is just Python with cell markers:

    # %% [markdown]
    # Prose goes here, as comments.

    # %%
    code_goes_here()

Why not just commit the .ipynb files? Because .ipynb is JSON with embedded outputs and
execution counts. Two people running the same notebook produce a 400-line diff with no
semantic change, review is impossible, and merge conflicts are unresolvable. The .py
sources are the artifact you version; the notebooks are a build output.

Stdlib only: no jupytext install required, which matters when a participant is fighting
their environment five minutes before the session starts.

    python scripts/build_notebooks.py          # build all
    python scripts/build_notebooks.py 02 04    # build only modules 02 and 04
    python scripts/build_notebooks.py --check   # verify up to date, exit 1 if not (CI)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = ROOT / "notebook_src"
OUT_DIR = ROOT / "notebooks"

CODE_MARKER = "# %%"
MARKDOWN_MARKER = "# %% [markdown]"
RAW_MARKER = "# %% [raw]"


def parse_cells(text: str) -> list[tuple[str, str]]:
    """Split percent-format source into (cell_type, source) pairs.

    Anything before the first marker is ignored, that is where the module docstring lives.
    """
    cells: list[tuple[str, str]] = []
    cell_type: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if cell_type is None:
            return
        source = "\n".join(buffer).strip("\n")
        if source.strip():
            cells.append((cell_type, source))

    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith(CODE_MARKER):
            flush()
            buffer = []
            if stripped.startswith(MARKDOWN_MARKER):
                cell_type = "markdown"
            elif stripped.startswith(RAW_MARKER):
                cell_type = "raw"
            else:
                cell_type = "code"
            continue
        if cell_type is not None:
            buffer.append(line)

    flush()
    return cells


def uncomment(source: str) -> str:
    """Strip the leading `# ` from a markdown cell's lines.

    Markdown cells are written as comments so the .py file stays valid Python, which means
    your editor, linter, and formatter all work on the workshop content.
    """
    lines = []
    for line in source.splitlines():
        if line.startswith("# "):
            lines.append(line[2:])
        elif line.strip() == "#":
            lines.append("")
        else:
            lines.append(line)
    return "\n".join(lines).strip("\n")


def cell_id(cell_type: str, body: str, seen: set[str]) -> str:
    """A stable id derived from the cell's own content.

    nbformat 4.5 requires an id on every cell. It has to be deterministic or `--check`
    would report drift on every run, and it has to come from the content rather than the
    index, or inserting one cell renumbers every id below it and we're back to the
    unreviewable diffs this whole build step exists to avoid.
    """
    digest = hashlib.sha1(f"{cell_type}\n{body}".encode()).hexdigest()[:12]
    candidate, n = digest, 1
    while candidate in seen:            # identical cells appear twice in a couple of modules
        candidate, n = f"{digest}-{n}", n + 1
    seen.add(candidate)
    return candidate


def to_notebook(cells: list[tuple[str, str]]) -> dict:
    nb_cells = []
    seen_ids: set[str] = set()
    for cell_type, source in cells:
        body = uncomment(source) if cell_type in {"markdown", "raw"} else source
        cell: dict = {
            "cell_type": cell_type,
            "id": cell_id(cell_type, body, seen_ids),
            "metadata": {},
            "source": body.splitlines(keepends=True) or [""],
        }
        if cell_type == "code":
            cell["outputs"] = []
            cell["execution_count"] = None
        nb_cells.append(cell)

    return {
        "cells": nb_cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3.11"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def build(source: Path, *, check: bool) -> bool:
    """Build one notebook. Returns True if the output was already current."""
    cells = parse_cells(source.read_text(encoding="utf-8"))
    if not cells:
        raise SystemExit(f"{source}: no `# %%` cell markers found, nothing to build.")

    notebook = to_notebook(cells)
    rendered = json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"

    target = OUT_DIR / f"{source.stem}.ipynb"
    current = target.read_text(encoding="utf-8") if target.exists() else None

    if current == rendered:
        print(f"  = {target.relative_to(ROOT)} (up to date)")
        return True

    if check:
        print(f"  ! {target.relative_to(ROOT)} is stale, run scripts/build_notebooks.py")
        return False

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    code = sum(1 for t, _ in cells if t == "code")
    print(f"  > {target.relative_to(ROOT)} ({len(cells)} cells, {code} code)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "modules",
        nargs="*",
        help="Module prefixes to build, e.g. 01 03. Builds everything if omitted.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Do not write; exit 1 if any notebook is out of date. For CI.",
    )
    args = parser.parse_args(argv)

    if not SRC_DIR.exists():
        raise SystemExit(f"No notebook_src/ directory at {SRC_DIR}")

    sources = sorted(SRC_DIR.glob("*.py"))
    if args.modules:
        wanted = tuple(args.modules)
        sources = [s for s in sources if s.stem.startswith(wanted)]
        if not sources:
            raise SystemExit(f"No sources matching {wanted} in {SRC_DIR}")

    print(f"{'Checking' if args.check else 'Building'} {len(sources)} notebook(s):")
    ok = all(build(source, check=args.check) for source in sources)

    if args.check and not ok:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
