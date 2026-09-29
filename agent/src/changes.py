"""Tracked file edits confined to the package tree.

Every modification goes through a ChangeSet so the Agent can revert, audit, and convert
upstream edits into packaging patches before exit. The evaluator accepts only UTF-8 text
changes, so binary or non-UTF-8 files are never written.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path


class EditError(ValueError):
    pass


_FORBIDDEN_PARTS = {".pc", ".git"}


class ChangeSet:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self._original: dict[str, bytes | None] = {}

    # -- path handling -------------------------------------------------------------------
    def resolve(self, relative: str | Path) -> Path:
        rel = Path(relative)
        if rel.is_absolute():
            try:
                rel = rel.resolve().relative_to(self.root)
            except ValueError as error:
                raise EditError(f"path is outside the package: {relative}") from error
        parts = rel.parts
        if not parts or any(p in ("", ".", "..") for p in parts) or _FORBIDDEN_PARTS & set(parts):
            raise EditError(f"refusing to touch path: {relative}")
        path = self.root.joinpath(*parts)
        probe = self.root
        for part in parts:
            probe = probe / part
            if probe.is_symlink():
                raise EditError(f"refusing to follow symlink: {relative}")
        return path

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix() if path.is_absolute() else Path(path).as_posix()

    # -- reads ---------------------------------------------------------------------------
    def read_text(self, relative: str | Path) -> str:
        data = self.resolve(relative).read_bytes()
        if b"\0" in data:
            raise EditError(f"binary file: {relative}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EditError(f"not UTF-8 text: {relative}") from error

    # -- writes --------------------------------------------------------------------------
    def _remember(self, path: Path) -> None:
        key = path.relative_to(self.root).as_posix()
        if key not in self._original:
            self._original[key] = path.read_bytes() if path.is_file() else None

    def write_text(self, relative: str | Path, text: str) -> None:
        path = self.resolve(relative)
        if path.exists() and not path.is_file():
            raise EditError(f"not a regular file: {relative}")
        if path.is_file():
            self.read_text(relative)  # rejects binary / non-UTF-8 targets
        if "\0" in text:
            raise EditError("content contains NUL bytes")
        self._remember(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = path.stat().st_mode if path.exists() else None
        path.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(path, mode)

    def delete(self, relative: str | Path) -> None:
        path = self.resolve(relative)
        if not path.is_file():
            raise EditError(f"no such file: {relative}")
        self.read_text(relative)
        self._remember(path)
        path.unlink()

    # -- bookkeeping ---------------------------------------------------------------------
    def original_text(self, relative: str) -> str | None:
        data = self._original.get(relative)
        return None if data is None else data.decode("utf-8")

    def modified(self) -> list[str]:
        changed = []
        for key, before in sorted(self._original.items()):
            path = self.root / key
            after = path.read_bytes() if path.is_file() else None
            if after != before:
                changed.append(key)
        return changed

    def revert(self, relative: str) -> None:
        before = self._original.get(relative)
        path = self.root / relative
        if before is None:
            if path.is_file():
                path.unlink()
        else:
            path.write_bytes(before)

    def revert_all(self) -> None:
        for key in list(self._original):
            self.revert(key)
        self._original.clear()

    def diff(self, relative: str) -> str:
        before = self.original_text(relative)
        path = self.root / relative
        after = path.read_text(encoding="utf-8") if path.is_file() else None
        return unified_diff(relative, before, after)


def unified_diff(relative: str, before: str | None, after: str | None) -> str:
    """Unified diff (-p1 style) that also marks a missing trailing newline."""
    old = [] if before is None else before.splitlines(keepends=True)
    new = [] if after is None else after.splitlines(keepends=True)
    lines = list(
        difflib.unified_diff(
            old,
            new,
            fromfile="/dev/null" if before is None else f"a/{relative}",
            tofile="/dev/null" if after is None else f"b/{relative}",
        )
    )
    out = []
    for line in lines:
        if line.endswith("\n"):
            out.append(line)
        else:
            out.append(line + "\n\\ No newline at end of file\n")
    return "".join(out)
