#!/usr/bin/env python3
"""Summarize Build-Bench development cases: source formats, failure stages, error signatures.

Usage: analyze_cases.py DATASET_ROOT [--csv OUT.csv]
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
from pathlib import Path

ERROR_PATTERNS = [
    ("arch-not-supported", r"not in arch list|Architecture: .* not supported|does not support the .* architecture|unsupported architecture|Unsupported architecture"),
    ("dep-unsatisfiable", r"unsat-dependency|Depends: .* but it is not (going to be )?installable|unmet dependencies|Could not satisfy build-dependency|build-dependencies could not be satisfied|dependency installability problem"),
    ("dh-missing-files", r"dh_install: .*missing files|dh_missing: .*(not installed|missing files)|cp: cannot stat .*debian/tmp"),
    ("symbols-mismatch", r"dpkg-gensymbols: .*(error|warning): some (new )?symbols|NOTE: see diff output below|dpkg-gensymbols: error"),
    ("test-failure", r"(FAIL|failed)[: ].*tests?|Test.*failed|make(\[\d+\])?: \*\*\* \[.*(check|test).*\] Error|dh_auto_test: error|tests? failed"),
    ("compile-error", r"error: |Error: .*\.(c|cc|cpp|h):|fatal error:"),
    ("link-error", r"undefined reference to|relocation .* against|ld(\.\w+)?: .*error"),
    ("asm-intrinsics", r"immintrin|xmmintrin|arm_neon|__builtin_ia32|-msse|-mavx|-mfpu|unrecognized command[- ]line option|unknown architecture|Unknown CPU|invalid instruction"),
    ("timeout-or-killed", r"Terminated|Killed|timed out|timeout|No space left"),
    ("missing-file", r"No such file or directory|No rule to make target"),
]


TAIL_BYTES = 4 << 20  # some logs exceed 2 GB; the sbuild summary and final errors sit at the end


def read_tail(path: Path, limit: int = TAIL_BYTES) -> str:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - limit))
        return fh.read().decode("utf-8", errors="replace")


def fail_stage(log: str) -> str:
    m = re.search(r"^Fail-Stage:\s*(\S+)", log, re.M)
    return m.group(1) if m else "unknown"


def dsc_format(input_dir: Path) -> str:
    for dsc in input_dir.glob("*.dsc"):
        m = re.search(r"^Format:\s*(.+)$", dsc.read_text(errors="replace"), re.M)
        if m:
            return m.group(1).strip()
    return "none"


def error_tail(log: str) -> str:
    """The last few meaningful lines before sbuild's cleanup section."""
    body = log.split("Build finished at", 1)[0]
    lines = [ln for ln in body.splitlines() if ln.strip() and not ln.startswith(("+---", "|"))]
    return "\n".join(lines[-25:])


def classify(tail: str) -> list[str]:
    return [name for name, pat in ERROR_PATTERNS if re.search(pat, tail)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--csv", type=Path)
    args = ap.parse_args()

    rows = []
    for case_json in sorted(args.root.glob("cases/*/*/case.json")):
        case = json.loads(case_json.read_text())
        cdir = case_json.parent
        log_path = cdir / case["paths"]["target_failure_log"]
        log = read_tail(log_path)
        tail = error_tail(log)
        rows.append({
            "case_id": case["case_id"],
            "direction": case["platform"]["direction"],
            "series": case["platform"]["series"],
            "package": case["package"]["name"],
            "format": dsc_format(cdir / case["paths"]["source_input"]),
            "stage": fail_stage(log),
            "tags": "|".join(classify(tail)) or "other",
            "log_bytes": log_path.stat().st_size,
            "last_line": tail.splitlines()[-1][:160] if tail else "",
        })

    def count(key: str) -> None:
        c = collections.Counter(r[key] for r in rows)
        print(f"\n== {key} ==")
        for k, v in c.most_common():
            print(f"{v:5d}  {k}")

    print(f"cases: {len(rows)}")
    for key in ("direction", "series", "format", "stage"):
        count(key)
    tags = collections.Counter(t for r in rows for t in r["tags"].split("|"))
    print("\n== error tags (multi-label) ==")
    for k, v in tags.most_common():
        print(f"{v:5d}  {k}")

    if args.csv:
        with args.csv.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
