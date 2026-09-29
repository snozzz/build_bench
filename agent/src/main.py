"""archfix-agent entrypoint: diagnose the target-architecture failure and repair the worktree."""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

from . import context as context_mod
from . import finalize
from .changes import ChangeSet
from .fixers import FIXERS

REPORT_NAME = "archfix-report.json"


def log(message: str) -> None:
    print(f"[archfix] {message}", file=sys.stderr, flush=True)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def repair(ctx: context_mod.Context) -> tuple[ChangeSet | None, list[str]]:
    pkg = ctx.package
    log(f"package: {pkg.kind} {pkg.name} {pkg.version} ({pkg.source_format}) at {pkg.root}")
    log(f"arch: {ctx.source_arch.isa if ctx.source_arch else '?'} -> {ctx.target_arch.isa if ctx.target_arch else '?'}")
    log(f"log: {ctx.log.path} ({ctx.log.size} bytes), failing command: {ctx.log.failing_command}")
    if pkg.kind not in ("deb", "rpm"):
        return None, [f"unsupported package layout: {pkg.kind}"]

    changes = ChangeSet(pkg.root)
    notes: list[str] = []
    for fixer in FIXERS:
        name = fixer.__name__.rsplit(".", 1)[-1]
        if not fixer.applies(ctx):
            continue
        try:
            fixer_notes = fixer.apply(ctx, changes)
        except Exception as error:  # a broken fixer must not sink the whole repair
            log(f"fixer {name} failed: {error!r}")
            continue
        notes += [f"{name}: {n}" for n in fixer_notes]

    patch = finalize.quilt_upstream_edits(pkg, changes, ctx.target_arch, "; ".join(notes))
    if patch:
        notes.append(f"upstream edits recorded as {patch}")
    return changes, notes


def main() -> int:
    started = time.monotonic()
    workspace = Path(os.environ.get("BB_WORKSPACE", "/workspace"))
    output = workspace / "output"
    result: dict[str, object] = {"schema_version": "0.1", "status": "completed"}
    report: dict[str, object] = {}
    changes: ChangeSet | None = None
    try:
        ctx = context_mod.load(workspace)
        report["context"] = ctx.as_dict()
        changes, notes = repair(ctx)
        modified = changes.modified() if changes else []
        report["notes"] = notes
        repo_prefix = ctx.package.root.resolve().relative_to(ctx.repo.resolve())
        result["modified_paths"] = [(repo_prefix / p).as_posix() for p in modified]
        result["message"] = "; ".join(notes) if notes else "No applicable repair was found."
    except Exception as error:
        log("internal error:\n" + traceback.format_exc())
        if changes is not None:
            changes.revert_all()  # never leave a half-applied repair behind
        result["message"] = f"Agent internal error: {error!r}"
        report["error"] = traceback.format_exc()
    report["elapsed_seconds"] = round(time.monotonic() - started, 2)
    try:
        _write_json(output / REPORT_NAME, report)
    except OSError as error:
        log(f"could not write report: {error!r}")
    _write_json(output / "agent-result.json", result)
    log(str(result["message"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
