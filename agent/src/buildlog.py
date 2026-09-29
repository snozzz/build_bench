"""Failure-log analysis that stays within a small memory budget.

Logs can be gigabytes; only the tail is read. The report isolates the final error cascade,
the first concrete errors that precede it, and the source locations they mention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TAIL_BYTES = 8 << 20
EXCERPT_CHARS = 24_000

_SUMMARY_FIELD = re.compile(r"^(Host Architecture|Build Architecture|Machine Architecture|Fail-Stage|Package|Version|Distribution|Status):\s*(.+)$", re.M)
_CASCADE = re.compile(
    r"^(?:make(?:\[\d+\])?: \*\*\*|dh_\w+(?:\.\w+)?: error:|dpkg-buildpackage: error|debian/rules:\d+: |"
    r"E: Build failure|error: Bad exit status|ninja: build stopped|cmake --build .* failed|"
    r"dh: error:|dpkg-source: error|Build killed|FATAL:)"
)
_PRIMARY = re.compile(
    r"(?:\berror:|\bError:|\bERROR\b|fatal error|undefined reference|multiple definition|"
    r"cannot find -l|relocation .* against|\bFAIL(?:ED)?\b|Traceback \(most recent call last\)|"
    r"Segmentation fault|Illegal instruction|Bus error|Aborted|Assertion .* failed|"
    r"No rule to make target|No such file or directory|not found|unrecognized (?:command[- ]line )?option|"
    r"unknown (?:type name|mnemonic|architecture)|#MISSING:|disappeared in the symbols file|"
    r"Cannot find \(any matches for\)|missing files|Test.*failed|tests? failed|Killed|timed out)"
)
_NOISE = re.compile(r"^(?:make(?:\[\d+\])?: (?:Entering|Leaving) directory|\s*$|-{20,}|\+-{20,}|\|)")
_FILE_REF = re.compile(
    r"(?:^|[\s'\"(\[])((?:/<<PKGBUILDDIR>>/|/<<BUILDDIR>>/[^/\s]+/|\./|\.\./)?"
    r"[\w.+-]+(?:/[\w.+-]+)*\.(?:c|cc|cpp|cxx|c\+\+|h|hh|hpp|hxx|inl|S|s|asm|rs|go|py|pyx|pxd|"
    r"f|f90|F90|java|hs|ml|mli|v|cmake|am|ac|mk|pl|pm|rb|js|ts|m4|sh|symbols|install|rules|pro|pri|"
    r"toml|build|in|txt)):(\d+)"
)
_BUILDDIR_PREFIX = re.compile(r"^(?:/<<PKGBUILDDIR>>/|/<<BUILDDIR>>/[^/]+/|\./)")


def read_tail(path: Path, limit: int = TAIL_BYTES) -> str:
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - limit))
        data = handle.read()
    text = data.decode("utf-8", errors="replace")
    if size > limit:
        text = text.split("\n", 1)[-1]  # drop the partial first line
    return text


@dataclass
class LogReport:
    path: Path | None
    size: int = 0
    summary: dict[str, str] = field(default_factory=dict)
    failing_command: str | None = None
    first_errors: list[str] = field(default_factory=list)
    file_refs: list[tuple[str, int]] = field(default_factory=list)
    excerpt: str = ""
    tail: str = ""

    @property
    def host_arch(self) -> str | None:
        return self.summary.get("Host Architecture") or self.summary.get("Build Architecture")

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path else None,
            "size": self.size,
            "summary": self.summary,
            "failing_command": self.failing_command,
            "first_errors": self.first_errors,
            "file_refs": [f"{p}:{n}" for p, n in self.file_refs],
        }


def _build_section(text: str) -> list[str]:
    """Lines of the build proper, without sbuild's trailing cleanup and summary."""
    for marker in ("\nBuild finished at ", "\nE: Build failure", "\n| Cleanup "):
        index = text.rfind(marker)
        if index != -1:
            end = text.find("\n", index + 1)
            text = text[: end if end != -1 else len(text)]
            break
    return text.splitlines()


def _cascade_start(lines: list[str]) -> int:
    """Index of the first line of the trailing make/dh/dpkg error cascade."""
    last = None
    for index in range(len(lines) - 1, max(-1, len(lines) - 400), -1):
        if _CASCADE.search(lines[index]):
            last = index
            break
    if last is None:
        return len(lines)
    start = last
    for index in range(last, max(-1, last - 60), -1):
        line = lines[index]
        if _CASCADE.search(line) or _NOISE.match(line):
            start = index
        else:
            break
    return start


def _normalize_ref(path: str) -> str:
    return _BUILDDIR_PREFIX.sub("", path)


def analyze(path: Path | None) -> LogReport:
    if path is None or not path.is_file():
        return LogReport(path=path)
    text = read_tail(path)
    report = LogReport(path=path, size=path.stat().st_size)
    report.summary = {k: v.strip() for k, v in _SUMMARY_FIELD.findall(text[-20_000:])}

    lines = _build_section(text)
    start = _cascade_start(lines)
    for line in lines[start:]:
        match = re.match(r"^(dh_\w+(?:\.\w+)?|dpkg-\w+): error", line)
        if match:
            report.failing_command = match.group(1)
            break

    # Concrete errors within the window that precedes the cascade.
    window_start = max(0, start - 4000)
    seen: set[str] = set()
    blocks: list[str] = []
    for index in range(window_start, start):
        line = lines[index]
        if not _PRIMARY.search(line) or _NOISE.match(line) or _CASCADE.search(line):
            continue
        key = re.sub(r"\d+", "#", line.strip())[:200]
        if key in seen:
            continue
        seen.add(key)
        report.first_errors.append(line.strip()[:300])
        context = lines[max(window_start, index - 3) : index + 4]
        blocks.append("\n".join(c[:400] for c in context))
        if len(report.first_errors) >= 40:
            break

    refs: list[tuple[str, int]] = []
    for line in report.first_errors:
        for match in _FILE_REF.finditer(line):
            ref = (_normalize_ref(match.group(1)), int(match.group(2)))
            if ref not in refs:
                refs.append(ref)
    report.file_refs = refs[:30]

    final = "\n".join(line[:400] for line in lines[max(0, start - 120) : start + 40])
    head = "\n".join(f"{k}: {v}" for k, v in report.summary.items())
    first = "\n...\n".join(blocks[:25])
    excerpt = (
        f"== summary ==\n{head}\n\n== first errors before the failure ==\n{first}\n\n"
        f"== last lines of the build ==\n{final}\n"
    )
    if len(excerpt) > EXCERPT_CHARS:
        keep = EXCERPT_CHARS // 2
        excerpt = excerpt[:keep] + "\n[... trimmed ...]\n" + excerpt[-keep:]
    report.excerpt = excerpt
    report.tail = text[-400_000:]
    return report
