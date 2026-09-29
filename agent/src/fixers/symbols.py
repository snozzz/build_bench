"""Deterministic repair of dpkg-gensymbols mismatches.

When the target architecture does not export symbols listed in debian/*.symbols,
dpkg-gensymbols fails and prints a diff where each lost entry reads
`+#MISSING: <version># <original line>`. Marking those entries `optional` is the standard
Debian treatment for architecture-dependent symbols (templates, SIMD variants, ...).
New symbols only fail the build at check level 4; those are added with an arch tag.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..changes import ChangeSet, EditError, split_lines
from ..context import Context

_BLOCK_HEADER = re.compile(r"^--- (debian/\S+) \(")
_MISSING = re.compile(r"^\+#MISSING: [^#]*# (.+)$")
_NEW = re.compile(r"^\+ (\S.*)$")
_DIFF_LINE = re.compile(r"^(?:\+\+\+ |@@ | |\+|-)")
_NEW_IS_ERROR = re.compile(r"dpkg-gensymbols: error: some new symbols appeared")


@dataclass
class SymbolsDiff:
    file: str
    missing: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)


def parse(text: str) -> list[SymbolsDiff]:
    diffs: list[SymbolsDiff] = []
    current: SymbolsDiff | None = None
    for line in text.splitlines():
        header = _BLOCK_HEADER.match(line)
        if header:
            current = SymbolsDiff(file=header.group(1))
            diffs.append(current)
            continue
        if current is None:
            continue
        if not _DIFF_LINE.match(line):
            current = None
            continue
        missing = _MISSING.match(line)
        if missing:
            current.missing.append(missing.group(1).strip())
            continue
        new = _NEW.match(line)
        if new and not line.startswith("+++ "):
            current.new.append(new.group(1).strip())
    # The same file can be reported more than once; keep the last report per file.
    latest: dict[str, SymbolsDiff] = {}
    for diff in diffs:
        latest[diff.file] = diff
    return [d for d in latest.values() if d.missing or d.new]


def add_tag(line: str, tag: str) -> str:
    """Add a dpkg-gensymbols tag to a symbols-file entry, preserving existing tags."""
    indent = line[: len(line) - len(line.lstrip())]
    body = line.lstrip()
    if body.startswith("("):
        close = body.find(")")
        tags = body[1:close].split("|")
        if tag in tags:
            return line
        return f"{indent}({body[1:close]}|{tag}){body[close + 1:]}"
    return f"{indent}({tag}){body}"


def _candidate_files(changes: ChangeSet, preferred: str) -> list[str]:
    files = [preferred]
    debian = changes.root / "debian"
    for path in sorted(debian.glob("*.symbols*")) + sorted(debian.glob("**/*.symbols*")):
        rel = path.relative_to(changes.root).as_posix()
        if path.is_file() and rel not in files:
            files.append(rel)
    return files


def applies(ctx: Context) -> bool:
    return ctx.package.kind == "deb" and "#MISSING:" in ctx.log.tail


def apply(ctx: Context, changes: ChangeSet) -> list[str]:
    notes: list[str] = []
    new_is_error = bool(_NEW_IS_ERROR.search(ctx.log.tail))
    for diff in parse(ctx.log.tail):
        pending = {entry: False for entry in diff.missing}
        for rel in _candidate_files(changes, diff.file):
            if all(pending.values()):
                break
            try:
                text = changes.read_text(rel)
            except (EditError, OSError):
                continue
            lines = split_lines(text)
            touched = 0
            for index, line in enumerate(lines):
                stripped = line.strip()
                if stripped in pending and not pending[stripped]:
                    ending = line[len(line.rstrip("\r\n")):]
                    lines[index] = add_tag(line.rstrip("\r\n"), "optional") + ending
                    pending[stripped] = True
                    touched += 1
            if touched:
                changes.write_text(rel, "".join(lines))
                notes.append(f"{rel}: marked {touched} symbol(s) missing on the target optional")
        unresolved = [e for e, done in pending.items() if not done]
        if unresolved:
            notes.append(f"{diff.file}: {len(unresolved)} missing symbol(s) not found in symbols files")

        if new_is_error and diff.new and ctx.target_arch:
            try:
                text = changes.read_text(diff.file)
            except (EditError, OSError):
                continue
            lines = split_lines(text)
            header = next((i for i, l in enumerate(lines) if l and not l[0].isspace() and not l.startswith(("#", "|", "*"))), None)
            if header is None:
                continue
            added = [f" (arch={ctx.target_arch.deb}){entry}\n" for entry in diff.new]
            lines[header + 1 : header + 1] = added
            changes.write_text(diff.file, "".join(lines))
            notes.append(f"{diff.file}: added {len(added)} new {ctx.target_arch.deb}-only symbol(s)")
    return notes
