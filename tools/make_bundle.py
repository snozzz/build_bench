#!/usr/bin/env python3
"""Build a Development Validation upload ZIP from harness runs.

Layout (website guide, suite development-recommended-v1):
  manifest.json                       {"schema_version": "0.1", "suite_id": ..., "cases": [...]}
  cases/<case-id>/worktree/input/...  repaired input, one format per Case

Formats:
  source  rebuilt source package from `harness pack` (.dsc + every referenced archive)
  tree    the Agent's repaired unpacked tree (exercises the platform's own source assembly);
          .pc/ is kept, Cases whose tree has symlinks fall back to `source`.

Usage: make_bundle.py RUNS_DIR OUT.zip [--format source|tree] [--filter REGEX]
       [--only-changed] [--suite development-recommended-v1]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path


def has_symlinks(root: Path) -> bool:
    return any(p.is_symlink() for p in root.rglob("*"))


def add_tree(zf: zipfile.ZipFile, src: Path, dest: str) -> int:
    count = 0
    for path in sorted(src.rglob("*")):
        if path.is_file() and not path.is_symlink():
            arc = f"{dest}/{path.relative_to(src).as_posix()}"
            info = zipfile.ZipInfo(arc, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = ((0o755 if os.access(path, os.X_OK) else 0o644) | 0o100000) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())
            count += 1
    return count


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--format", choices=("source", "tree"), default="source")
    ap.add_argument("--filter", default="")
    ap.add_argument("--only-changed", action="store_true", help="skip runs without repair.diff")
    ap.add_argument("--suite", default="development-recommended-v1")
    args = ap.parse_args()

    cases = []
    with zipfile.ZipFile(args.out, "w") as zf:
        for run in sorted(p for p in args.runs.iterdir() if (p / "case-dir").is_file()):
            case_id = run.name
            if not re.search(args.filter, case_id):
                continue
            if args.only_changed and not (run / "repair.diff").is_file():
                continue
            dest = f"cases/{case_id}/worktree/input"
            tree = run / "workspace" / "work" / "repo" / "input"
            fmt = args.format
            if fmt == "tree" and (not tree.is_dir() or has_symlinks(tree)):
                fmt = "source"
            if fmt == "source":
                src = run / "source-package"
                if not src.is_dir():
                    print(f"skip {case_id}: no source-package (run `harness pack` or keep runs)", file=sys.stderr)
                    continue
            else:
                src = tree
            files = add_tree(zf, src, dest)
            cases.append({"case_id": case_id, "worktree": f"cases/{case_id}/worktree"})
            print(f"{case_id}: {fmt}, {files} files")
        manifest = {"schema_version": "0.1", "suite_id": args.suite, "cases": cases}
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
    print(f"{len(cases)} case(s) -> {args.out} ({args.out.stat().st_size / 1e6:.1f} MB)")
    return 0 if cases else 1


if __name__ == "__main__":
    raise SystemExit(main())
