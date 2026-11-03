#!/usr/bin/env python3
"""Refresh the gh-pages root branch-selector entry page.

Copies ``docs/branch_selector.html`` to ``<site_root>/index.html``, ensures
``.nojekyll`` exists (so GitHub Pages / Jekyll does not hide Sphinx ``_static``
paths), and writes ``branches.json`` from every ``build_info.json`` found under
a branch folder.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent
DEFAULT_SELECTOR = DOCS_DIR / "branch_selector.html"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "site_root",
        type=Path,
        help="Checkout of the gh-pages branch (e.g. www)",
    )
    parser.add_argument(
        "--selector",
        type=Path,
        default=DEFAULT_SELECTOR,
        help=f"Branch selector HTML (default: {DEFAULT_SELECTOR})",
    )
    args = parser.parse_args()

    site_root = args.site_root.resolve()
    selector = args.selector.resolve()
    if not site_root.is_dir():
        raise SystemExit(f"site root is not a directory: {site_root}")
    if not selector.is_file():
        raise SystemExit(f"selector page not found: {selector}")

    shutil.copyfile(selector, site_root / "index.html")
    (site_root / ".nojekyll").touch()

    branches = sorted(
        p.parent.relative_to(site_root).as_posix()
        for p in site_root.rglob("build_info.json")
        if p.parent != site_root
    )
    (site_root / "branches.json").write_text(
        json.dumps(branches, indent=2) + "\n", encoding="utf-8"
    )
    print("Published branches:", ", ".join(branches) or "(none)")


if __name__ == "__main__":
    main()
