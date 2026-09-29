"""Turn tracked edits into a repair that survives the platform's source repacking.

For Debian "3.0 (quilt)" trees, edits to upstream files are moved into a new quilt patch
appended to the series and the upstream files are restored. `dpkg-source -b` applies
unapplied series entries before building, so this works whether or not the tree was
extracted with patches applied. Other formats keep direct edits.
"""

from __future__ import annotations

from .arch import Arch
from .changes import ChangeSet, unified_diff
from .package import Package

PATCH_STEM = "archfix"


def _series_files(changes: ChangeSet) -> list[str]:
    patches = changes.root / "debian" / "patches"
    names = ["debian/patches/series"]
    if patches.is_dir():
        names += sorted(
            f"debian/patches/{p.name}" for p in patches.glob("*.series") if p.is_file()
        )
    return names


def _patch_name(changes: ChangeSet, arch: Arch | None) -> str:
    base = f"{PATCH_STEM}-{arch.deb if arch else 'target'}-build"
    patches = changes.root / "debian" / "patches"
    name = f"{base}.patch"
    counter = 2
    while (patches / name).exists():
        name = f"{base}-{counter}.patch"
        counter += 1
    return name


def quilt_upstream_edits(
    pkg: Package, changes: ChangeSet, arch: Arch | None, description: str
) -> str | None:
    """Move upstream edits into debian/patches; returns the patch path or None."""
    if not pkg.uses_quilt:
        return None
    upstream = [p for p in changes.modified() if not p.startswith("debian/")]
    if not upstream:
        return None

    body = []
    for rel in upstream:
        path = changes.root / rel
        after = path.read_text(encoding="utf-8") if path.is_file() else None
        body.append(unified_diff(rel, changes.original_text(rel), after))
    target = arch.deb if arch else "the target architecture"
    summary = " ".join(description.split())[:600] or f"Fix the build on {target}."
    header = (
        f"Description: Fix build failure on {target}\n"
        f" {summary}\n"
        "Author: archfix-agent\n"
        "Forwarded: not-needed\n"
        "---\n"
    )
    name = _patch_name(changes, arch)
    patch_rel = f"debian/patches/{name}"
    changes.write_text(patch_rel, header + "".join(body))

    for series in _series_files(changes):
        path = changes.root / series
        if series != "debian/patches/series" and not path.is_file():
            continue
        current = path.read_text(encoding="utf-8") if path.is_file() else ""
        if current and not current.endswith("\n"):
            current += "\n"
        changes.write_text(series, current + name + "\n")

    for rel in upstream:
        changes.revert(rel)
    return patch_rel
