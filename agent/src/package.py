"""Locate and describe the package inside the writable worktree."""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

_SKIP_DIRS = {".pc", ".git", "node_modules", "__pycache__"}
_MAX_DEPTH = 4


@dataclass
class Package:
    kind: str  # "deb" (unpacked tree), "deb-packed" (.dsc only), "rpm", or "unknown"
    root: Path
    name: str | None = None
    version: str | None = None
    source_format: str | None = None
    spec: Path | None = None

    @property
    def uses_quilt(self) -> bool:
        return self.kind == "deb" and (self.source_format or "").startswith("3.0 (quilt)")

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "root": str(self.root),
            "name": self.name,
            "version": self.version,
            "source_format": self.source_format,
            "spec": str(self.spec) if self.spec else None,
        }


def _walk(repo: Path):
    queue = deque([(repo, 0)])
    while queue:
        directory, depth = queue.popleft()
        yield directory
        if depth >= _MAX_DEPTH:
            continue
        try:
            children = sorted(p for p in directory.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            continue
        for child in children:
            if child.name not in _SKIP_DIRS:
                queue.append((child, depth + 1))


def _debian(root: Path) -> Package:
    pkg = Package(kind="deb", root=root)
    changelog = root / "debian" / "changelog"
    if changelog.is_file():
        first = changelog.read_text(encoding="utf-8", errors="replace").split("\n", 1)[0]
        match = re.match(r"^(\S+) \(([^)]+)\)", first)
        if match:
            pkg.name, pkg.version = match.groups()
    fmt = root / "debian" / "source" / "format"
    pkg.source_format = fmt.read_text(encoding="utf-8", errors="replace").strip() if fmt.is_file() else "1.0"
    return pkg


def _rpm(root: Path, spec: Path) -> Package:
    pkg = Package(kind="rpm", root=root, spec=spec)
    text = spec.read_text(encoding="utf-8", errors="replace")
    for key, attr in (("Name", "name"), ("Version", "version")):
        match = re.search(rf"^{key}:\s*(\S+)", text, re.M)
        if match:
            setattr(pkg, attr, match.group(1))
    return pkg


def detect(repo: Path) -> Package:
    specs: list[Path] = []
    dscs: list[Path] = []
    for directory in _walk(repo):
        if (directory / "debian" / "control").is_file():
            return _debian(directory)
        try:
            files = [p for p in directory.iterdir() if p.is_file()]
        except OSError:
            continue
        specs.extend(p for p in files if p.suffix == ".spec")
        dscs.extend(p for p in files if p.suffix == ".dsc")
    if specs:
        return _rpm(specs[0].parent, specs[0])
    if dscs:
        return Package(kind="deb-packed", root=dscs[0].parent)
    return Package(kind="unknown", root=repo)
