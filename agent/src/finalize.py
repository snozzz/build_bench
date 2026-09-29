"""Turn tracked edits into a repair that survives the platform's source repacking.

The platform rebuilds the Debian source package from the repaired tree with
`dpkg-source -b`, which aborts on upstream edits that no quilt patch records. For
"3.0 (quilt)" trees the upstream edits are therefore recorded as patch hunks:

* fold: when the series already has patches, the hunks are appended to the last patch.
  No file is created, so the repair does not depend on how the evaluator encodes new
  files. If the tree has the series applied (`.pc/applied-patches`), the upstream edits
  stay in place so the tree matches "orig + series"; otherwise they are reverted and
  `dpkg-source -b` applies the extended patch itself.
* new: without usable patches, a new patch is added to the series and the upstream edits
  are reverted; `dpkg-source -b` applies unapplied series entries before building.

Other source formats keep direct edits, which dpkg-source records by itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .arch import Arch
from .changes import ChangeSet, EditError, read_utf8, unified_diff
from .package import Package

PATCH_STEM = "archfix"
SERIES = "debian/patches/series"


@dataclass
class SeriesEntry:
    name: str
    strip: int  # -pN level


def _read_series(changes: ChangeSet) -> list[SeriesEntry] | None:
    path = changes.root / SERIES
    if not path.is_file():
        return None
    entries = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        strip = 1
        for option in fields[1:]:
            match = re.fullmatch(r"-p(\d+)", option)
            strip = int(match.group(1)) if match else -1
        entries.append(SeriesEntry(fields[0], strip))
    return entries


def _series_applied(changes: ChangeSet, last: SeriesEntry) -> bool:
    applied = changes.root / ".pc" / "applied-patches"
    if not applied.is_file():
        return False
    names = [l.strip() for l in applied.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    return bool(names) and names[-1] == last.name


def _hunks(changes: ChangeSet, upstream: list[str], strip: int) -> str:
    body = []
    for rel in upstream:
        path = changes.root / rel
        after = read_utf8(path) if path.is_file() else None
        text = unified_diff(rel, changes.original_text(rel), after)
        if strip == 0:
            text = re.sub(r"^(---|\+\+\+) [ab]/", r"\1 ", text, flags=re.M)
        body.append(text)
    return "".join(body)


def _new_patch_name(changes: ChangeSet, arch: Arch | None) -> str:
    base = f"{PATCH_STEM}-{arch.deb if arch else 'target'}-build"
    patches = changes.root / "debian" / "patches"
    name, counter = f"{base}.patch", 2
    while (patches / name).exists():
        name, counter = f"{base}-{counter}.patch", counter + 1
    return name


def _fold(changes: ChangeSet, upstream: list[str], entries: list[SeriesEntry]) -> str | None:
    last = entries[-1]
    if last.strip not in (0, 1):
        return None
    patch_rel = f"debian/patches/{last.name}"
    try:
        current = changes.read_text(patch_rel)
    except (EditError, OSError):
        return None
    applied = _series_applied(changes, last)
    if current and not current.endswith("\n"):
        current += "\n"
    changes.write_text(patch_rel, current + _hunks(changes, upstream, last.strip))
    if not applied:
        for rel in upstream:
            changes.revert(rel)
    state = "series applied, upstream edits kept" if applied else "upstream edits reverted"
    return f"upstream edits folded into {patch_rel} ({state})"


def _new_patch(changes: ChangeSet, upstream: list[str], arch: Arch | None, description: str) -> str:
    target = arch.deb if arch else "the target architecture"
    summary = " ".join(description.split())[:600] or f"Fix the build on {target}."
    header = (
        f"Description: Fix build failure on {target}\n"
        f" {summary}\n"
        "Author: archfix-agent\n"
        "Forwarded: not-needed\n"
        "---\n"
    )
    name = _new_patch_name(changes, arch)
    patch_rel = f"debian/patches/{name}"
    changes.write_text(patch_rel, header + _hunks(changes, upstream, 1))
    # Vendor series (e.g. ubuntu.series) take precedence over the plain one when present.
    vendor = sorted(p.relative_to(changes.root).as_posix() for p in (changes.root / "debian" / "patches").glob("*.series"))
    for series_rel in [SERIES] + vendor:
        series = changes.root / series_rel
        current = changes.read_text(series_rel) if series.is_file() else ""
        if current and not current.endswith("\n"):
            current += "\n"
        changes.write_text(series_rel, current + name + "\n")
    for rel in upstream:
        changes.revert(rel)
    return f"upstream edits recorded as new {patch_rel}"


def record_upstream_edits(
    pkg: Package, changes: ChangeSet, arch: Arch | None, description: str, strategy: str = "auto"
) -> str | None:
    """Make upstream edits representable for `dpkg-source -b`; returns a note or None."""
    if not pkg.uses_quilt:
        return None
    upstream = [p for p in changes.modified() if not p.startswith("debian/")]
    if not upstream:
        return None
    entries = _read_series(changes)
    vendor_series = list((changes.root / "debian" / "patches").glob("*.series"))
    if strategy in ("auto", "fold") and entries and not vendor_series:
        note = _fold(changes, upstream, entries)
        if note:
            return note
    return _new_patch(changes, upstream, arch, description)
