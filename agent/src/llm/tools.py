"""Tools exposed to the model. Reads are bounded; every write goes through ChangeSet."""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Callable

from ..changes import ChangeSet, EditError
from ..context import Context

MAX_READ_LINES = 400
MAX_OUTPUT_CHARS = 20_000
MAX_FILE_BYTES = 2_000_000
SKIP_DIRS = {".pc", ".git", "node_modules", "__pycache__"}


class ToolError(Exception):
    pass


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 200] + f"\n[... output truncated, {len(text) - limit + 200} more chars ...]"


def _schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


class Toolbox:
    def __init__(self, ctx: Context, changes: ChangeSet):
        self.ctx = ctx
        self.changes = changes
        self.root = changes.root
        self.finished: str | None = None
        self._log_lines: list[str] | None = None
        self.handlers: dict[str, Callable[..., str]] = {
            "list_dir": self.list_dir,
            "read_file": self.read_file,
            "search": self.search,
            "find_files": self.find_files,
            "search_log": self.search_log,
            "read_log": self.read_log,
            "replace_in_file": self.replace_in_file,
            "write_file": self.write_file,
            "show_changes": self.show_changes,
            "revert_file": self.revert_file,
            "finish": self.finish,
        }

    # -- schemas -------------------------------------------------------------------------
    def schemas(self) -> list[dict]:
        s = {"type": "string"}
        i = {"type": "integer"}
        return [
            _schema("list_dir", "List a directory of the package tree (paths are relative to the package root).",
                    {"path": s, "depth": i}, []),
            _schema("read_file", f"Read a text file with line numbers (at most {MAX_READ_LINES} lines per call).",
                    {"path": s, "start_line": i, "end_line": i}, ["path"]),
            _schema("search", "Regex search over text files in the package tree. Returns path:line: text.",
                    {"pattern": s, "path": s, "glob": s, "max_results": i}, ["pattern"]),
            _schema("find_files", "Find files whose name matches a glob (e.g. '*.symbols', 'CMakeLists.txt').",
                    {"name_glob": s, "max_results": i}, ["name_glob"]),
            _schema("search_log", "Regex search in the initial target-architecture build log (tail). Returns matches with context.",
                    {"pattern": s, "context": i, "max_results": i}, ["pattern"]),
            _schema("read_log", "Read log lines by line number of the loaded log tail (1-based; negative counts from the end).",
                    {"start_line": i, "count": i}, ["start_line"]),
            _schema("replace_in_file", "Replace an exact, unique text snippet in a file. Include enough context for uniqueness; whitespace must match exactly.",
                    {"path": s, "old": s, "new": s}, ["path", "old", "new"]),
            _schema("write_file", "Create or fully overwrite a text file. Prefer replace_in_file for existing files.",
                    {"path": s, "content": s}, ["path", "content"]),
            _schema("show_changes", "Show the unified diff of all edits made so far.", {}, []),
            _schema("revert_file", "Undo all edits to one file.", {"path": s}, ["path"]),
            _schema("finish", "Finish the repair. Summarize the root cause and the change.", {"summary": s}, ["summary"]),
        ]

    # -- dispatch ------------------------------------------------------------------------
    def call(self, name: str, arguments: str | dict) -> str:
        handler = self.handlers.get(name)
        if handler is None:
            return f"error: unknown tool {name!r}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
            if not isinstance(args, dict):
                raise ToolError("arguments must be a JSON object")
            return _clip(handler(**args))
        except (ToolError, EditError, OSError, ValueError, TypeError) as error:
            return f"error: {error}"

    # -- helpers -------------------------------------------------------------------------
    def _path(self, rel: str) -> Path:
        rel = (rel or ".").strip().lstrip("/") or "."
        path = (self.root / rel).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError(f"path outside the package: {rel}")
        return path

    def _iter_files(self, base: Path):
        for path in sorted(base.rglob("*")):
            rel_parts = path.relative_to(self.root).parts
            if SKIP_DIRS & set(rel_parts) or path.is_symlink() or not path.is_file():
                continue
            yield path

    def _log(self) -> list[str]:
        if self._log_lines is None:
            self._log_lines = self.ctx.log.tail.splitlines() if self.ctx.log.tail else []
        return self._log_lines

    # -- read tools ----------------------------------------------------------------------
    def list_dir(self, path: str = ".", depth: int = 1) -> str:
        base = self._path(path)
        if not base.is_dir():
            raise ToolError(f"not a directory: {path}")
        depth = max(1, min(int(depth), 3))
        out = []
        for entry in sorted(base.rglob("*")):
            rel = entry.relative_to(base)
            if len(rel.parts) > depth or SKIP_DIRS & set(rel.parts):
                continue
            suffix = "/" if entry.is_dir() else ("@" if entry.is_symlink() else "")
            out.append(rel.as_posix() + suffix)
            if len(out) >= 300:
                out.append("[... more entries ...]")
                break
        return "\n".join(out) or "(empty)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        target = self._path(path)
        if not target.is_file():
            raise ToolError(f"no such file: {path}")
        data = target.read_bytes()
        if b"\0" in data[:8192]:
            raise ToolError("binary file")
        lines = data.decode("utf-8", errors="replace").split("\n")
        start = max(1, int(start_line))
        end = min(len(lines), int(end_line) if end_line else start + MAX_READ_LINES - 1)
        end = min(end, start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n:6d}  {lines[n - 1]}" for n in range(start, end + 1))
        return f"{path} (lines {start}-{end} of {len(lines)})\n{body}"

    def search(self, pattern: str, path: str = ".", glob: str | None = None, max_results: int = 60) -> str:
        regex = re.compile(pattern)
        base = self._path(path)
        files = [base] if base.is_file() else self._iter_files(base)
        hits = []
        for file in files:
            if glob and not fnmatch.fnmatch(file.name, glob):
                continue
            if file.stat().st_size > MAX_FILE_BYTES:
                continue
            data = file.read_bytes()
            if b"\0" in data[:8192]:
                continue
            for number, line in enumerate(data.decode("utf-8", errors="replace").split("\n"), 1):
                if regex.search(line):
                    hits.append(f"{file.relative_to(self.root).as_posix()}:{number}: {line.strip()[:240]}")
                    if len(hits) >= max_results:
                        return "\n".join(hits) + "\n[... max_results reached ...]"
        return "\n".join(hits) or "no matches"

    def find_files(self, name_glob: str, max_results: int = 100) -> str:
        hits = [p.relative_to(self.root).as_posix() for p in self._iter_files(self.root)
                if fnmatch.fnmatch(p.name, name_glob)]
        return "\n".join(hits[:max_results]) or "no matches"

    def search_log(self, pattern: str, context: int = 2, max_results: int = 30) -> str:
        regex = re.compile(pattern)
        lines = self._log()
        context = max(0, min(int(context), 10))
        out = []
        for index, line in enumerate(lines):
            if regex.search(line):
                lo, hi = max(0, index - context), min(len(lines), index + context + 1)
                out.append("\n".join(f"{n + 1}: {lines[n][:300]}" for n in range(lo, hi)))
                if len(out) >= max_results:
                    break
        return "\n--\n".join(out) or "no matches"

    def read_log(self, start_line: int, count: int = 100) -> str:
        lines = self._log()
        start = int(start_line)
        if start < 0:
            start = max(1, len(lines) + start + 1)
        count = max(1, min(int(count), 300))
        return "\n".join(f"{n}: {lines[n - 1][:400]}" for n in range(start, min(len(lines), start + count - 1) + 1))

    # -- write tools ---------------------------------------------------------------------
    def replace_in_file(self, path: str, old: str, new: str) -> str:
        text = self.changes.read_text(path)
        count = text.count(old)
        if not old:
            raise ToolError("old must not be empty")
        if count == 0:
            raise ToolError("old text not found; re-read the file and copy the exact text")
        if count > 1:
            raise ToolError(f"old text occurs {count} times; add more context")
        self.changes.write_text(path, text.replace(old, new, 1))
        return f"ok: {path} updated"

    def write_file(self, path: str, content: str) -> str:
        self.changes.write_text(path, content)
        return f"ok: {path} written ({len(content)} chars)"

    def show_changes(self) -> str:
        return "".join(self.changes.diff(rel) for rel in self.changes.modified()) or "no changes"

    def revert_file(self, path: str) -> str:
        rel = self.changes.rel(self._path(path))
        self.changes.revert(rel)
        return f"ok: {rel} reverted"

    def finish(self, summary: str) -> str:
        self.finished = summary.strip() or "done"
        return "ok"
