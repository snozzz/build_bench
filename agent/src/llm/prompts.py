"""Prompt text and the initial Case dossier for the repair loop."""

from __future__ import annotations

from pathlib import Path

from ..context import Context

SYSTEM = """\
You are an expert Debian/Ubuntu and RPM package maintainer and a cross-architecture porting
engineer. A source package builds on its source architecture but its build on the target
architecture failed. Diagnose the root cause from the build log and the package sources, then
repair the package by editing files with the tools provided.

Hard constraints
- You cannot run builds or commands. Your edits are applied once and rebuilt by an official
  target-architecture build. Be precise; prefer the smallest correct change.
- Edit only files inside the package tree. Edit upstream source files directly when the fix
  belongs there; the harness records upstream edits as a quilt patch automatically. Do not
  hand-write files under debian/patches/ and do not touch .pc/.
- Competition policy forbids build bypasses: do not ignore or suppress failures wholesale
  (no `|| true` around build or test steps, no blanket DEB_BUILD_OPTIONS=nocheck, no removing
  test suites, no dummy artifacts, no dropping the package for the target architecture).
  Fix the real incompatibility. If a single test is genuinely architecture-specific or
  numerically fragile, narrowly skip or relax only that test on the target architecture and
  say why in a comment.
- Keep the package name, version and changelog unchanged. Do not change Build-Depends unless
  the failure is caused by them.
- Files use exact whitespace; debian/rules recipe lines start with a TAB.

Useful knowledge
- Debian arch names: amd64 (x86_64), arm64 (aarch64), riscv64. Conditionals in debian/rules:
  `include /usr/share/dpkg/architecture.mk` then `ifeq ($(DEB_HOST_ARCH),arm64)` or
  `ifneq (,$(filter amd64 i386,$(DEB_HOST_ARCH)))`; CPU family via DEB_HOST_ARCH_CPU.
- C/C++ guards: `#if defined(__x86_64__) || defined(__i386__)`, `__aarch64__`, `__riscv`
  (`__riscv_xlen == 64`). x86-only headers/intrinsics: xmmintrin.h, emmintrin.h, immintrin.h,
  cpuid.h, sys/io.h (inb/outb/iopl), __builtin_ia32_*, rdtsc. x86-only compiler flags:
  -msse*, -mavx*, -mfpmath=sse, -m64/-m32 on arm, -march=x86-64; arm-only: -mfpu=neon,
  -mcpu=..., arm_neon.h. Use portable fallbacks (__builtin_prefetch, generic C code paths) or
  guard the SIMD path.
- `char` is unsigned by default on arm64 and riscv64 (signed on x86): comparisons with
  negative values or EOF may break; use `signed char` or -fsigned-char when appropriate.
- Floating point: arm64 GCC contracts a*b+c into FMA by default, changing results; tests that
  compare exactly may need -ffp-contract=off or a tolerance. long double is 128-bit quad on
  arm64/riscv64 but 80-bit on x86; __float128 exists only on x86 GCC.
- riscv64 often needs -latomic for 8/16-byte atomics; page size, cache line and alignment
  assumptions differ across architectures.
- Library symbols files (debian/*.symbols[.arch]): symbols that differ per architecture are
  tagged, e.g. `(arch=amd64 i386)sym@Base 1.0`, `(arch=!arm64)...`, or `(optional)` for
  compiler-generated template instances.
- debian/*.install and dh_install paths: use multiarch wildcards (`usr/lib/*/libfoo.so.*`) or
  ${DEB_HOST_MULTIARCH} (with dh-exec) instead of hard-coded x86_64-linux-gnu /
  aarch64-linux-gnu; architecture-specific binaries may need `[arch]`-style handling.
- Tests that hard-code architecture names, instruction sets, or /proc/cpuinfo formats must
  be taught about the target architecture.
- Rust crates: cfg(target_arch = "x86_64") gates; Go: build tags and GOARCH-specific files
  (*_amd64.go); Python/Cython: setup.py flags such as -msse; CMake: CMAKE_SYSTEM_PROCESSOR
  checks.

Workflow
1. Read the dossier. Find the FIRST real error that caused the failure (later errors are often
   consequences). Use search_log / read_log for more of the log when needed.
2. Inspect the relevant files (read_file, search) before editing. Confirm the exact text.
3. Make the fix with replace_in_file (preferred) or write_file.
4. Review with show_changes, then call finish with a short root-cause summary.
Be efficient: a typical repair needs 5-20 tool calls.
"""


def _read(path: Path, limit: int) -> str | None:
    if not path.is_file():
        return None
    text = path.read_bytes().decode("utf-8", errors="replace")
    if len(text) > limit:
        text = text[:limit] + "\n[... truncated ...]"
    return text


def _snippet(root: Path, ref: str, line: int, radius: int = 12) -> str | None:
    candidate = root / ref
    if not candidate.is_file():
        name = Path(ref).name
        tail = Path(ref).parts[-2:]
        matches = [p for p in root.rglob(name)
                   if ".pc" not in p.parts and p.is_file() and p.parts[-len(tail):] == tail]
        if len(matches) != 1:
            return None
        candidate = matches[0]
    lines = candidate.read_bytes().decode("utf-8", errors="replace").split("\n")
    lo, hi = max(1, line - radius), min(len(lines), line + radius)
    rel = candidate.relative_to(root).as_posix()
    body = "\n".join(f"{n:6d}{'>' if n == line else ' '} {lines[n - 1]}" for n in range(lo, hi + 1))
    return f"--- {rel}:{line}\n{body}"


def dossier(ctx: Context, notes: list[str]) -> str:
    pkg = ctx.package
    root = pkg.root
    src = ctx.source_arch
    dst = ctx.target_arch
    parts = [
        "# Case",
        f"package: {pkg.name} {pkg.version} ({pkg.kind}, source format {pkg.source_format})",
        f"migration: {src.isa if src else '?'} ({src.deb if src else '?'}) -> "
        f"{dst.isa if dst else '?'} ({dst.deb if dst else '?'})",
        f"failing command: {ctx.log.failing_command or 'unknown'}",
        f"distribution: {ctx.log.summary.get('Distribution', '?')}",
    ]
    if notes:
        parts += ["", "# Deterministic fixes already applied", *notes]
    parts += ["", "# Build log excerpt", ctx.log.excerpt or "(log unavailable)"]

    if pkg.kind == "deb":
        for rel, limit in (("debian/control", 6000), ("debian/rules", 6000),
                           ("debian/patches/series", 2000)):
            text = _read(root / rel, limit)
            if text is not None:
                parts += ["", f"# {rel}", text]
    elif pkg.kind == "rpm" and pkg.spec:
        text = _read(pkg.spec, 12000)
        parts += ["", f"# {pkg.spec.relative_to(root).as_posix()}", text or ""]

    snippets = []
    for ref, line in ctx.log.file_refs[:10]:
        snippet = _snippet(root, ref, line)
        if snippet and snippet not in snippets:
            snippets.append(snippet)
        if len(snippets) >= 6:
            break
    if snippets:
        parts += ["", "# Source around error locations", *snippets]

    top = sorted(p.name + ("/" if p.is_dir() else "") for p in root.iterdir() if p.name != ".pc")
    parts += ["", "# Package root", " ".join(top[:150])]
    parts += ["", "Paths in tools are relative to the package root. Repair the build."]
    return "\n".join(parts)
