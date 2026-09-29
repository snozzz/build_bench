#!/usr/bin/env python3
"""Round-trip test of the Agent's patch recording on real Development Cases.

For each Case: prepare the workspace, append a marker line to one upstream text file via
ChangeSet, run finalize.record_upstream_edits, derive the canonical diff, replay + repack
with dpkg-source -b, re-extract the rebuilt source, and check the marker is present.

Usage: selftest_finalize.py DATASET OUT [--filter REGEX] [--jobs N] [--strategy auto|new]
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import re
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "agent"))

import harness  # noqa: E402
from src.arch import lookup  # noqa: E402
from src.changes import _RISKY_BREAKS, ChangeSet, EditError  # noqa: E402
from src.finalize import record_upstream_edits  # noqa: E402
from src.package import detect  # noqa: E402

MARKER = "archfix-selftest-marker"
SUFFIXES = (".c", ".h", ".cc", ".cpp", ".py", ".txt", ".md", ".in", ".am", ".rs", ".go", ".java", ".R")


def pick_file(changes: ChangeSet) -> str | None:
    for path in sorted(changes.root.rglob("*")):
        rel = path.relative_to(changes.root).as_posix()
        if rel.startswith(("debian/", ".pc/")) or path.is_symlink() or not path.is_file():
            continue
        if path.suffix not in SUFFIXES or path.stat().st_size > 200_000:
            continue
        try:
            text = changes.read_text(rel)
        except EditError:
            continue
        if text.endswith("\n") and not _RISKY_BREAKS.search(text):
            return rel
    return None


def one(case_dir: Path, run: Path, strategy: str) -> tuple[str, bool, str]:
    try:
        harness.prepare(case_dir, run)
        pkg = detect(run / "workspace" / "work" / "repo")
        changes = ChangeSet(pkg.root)
        rel = pick_file(changes)
        if rel is None:
            return case_dir.name, True, "skip: no suitable upstream text file"
        changes.write_text(rel, changes.read_text(rel) + f"/* {MARKER} */\n")
        note = record_upstream_edits(pkg, changes, lookup("x86_64"), "selftest", strategy) or "direct edit"
        ok, info = harness.canonical_diff(run)
        if not ok:
            return case_dir.name, False, f"diff: {info}"
        ok, info = harness.pack(run)
        if not ok:
            return case_dir.name, False, f"pack: {info}"
        dsc = next((run / "source-package").glob("*.dsc"))
        with tempfile.TemporaryDirectory(dir=run) as tmp:
            res = harness.sh(["dpkg-source", "--no-check", "-x", str(dsc), str(Path(tmp) / "t")])
            if res.returncode != 0:
                return case_dir.name, False, "re-extract: " + res.stderr[-300:]
            content = (Path(tmp) / "t" / rel).read_text(encoding="utf-8", errors="replace")
        new = " +new" if harness.creates_files(run) else ""
        return case_dir.name, MARKER in content, f"{pkg.source_format}: {note}{new}"
    except Exception as error:
        return case_dir.name, False, f"error: {error!r}"
    finally:
        shutil.rmtree(run, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--filter", default="")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--strategy", default="auto")
    args = ap.parse_args()
    cases = [c for c in harness.case_dirs(args.dataset) if re.search(args.filter, c.name)]
    args.out.mkdir(parents=True, exist_ok=True)
    failures = 0
    kinds: dict[str, int] = {}
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(one, c, (args.out / c.name).resolve(), args.strategy) for c in cases]
        for future in cf.as_completed(futures):
            name, ok, info = future.result()
            key = re.sub(r"debian/patches/\S+", "debian/patches/<p>", info)
            kinds[key] = kinds.get(key, 0) + 1
            if not ok:
                failures += 1
                print(f"FAIL {name}: {info}", flush=True)
    for key, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"{count:4d}  {key[:160]}")
    print(f"failures: {failures}/{len(cases)}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
