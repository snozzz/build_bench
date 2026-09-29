"""Everything the Agent knows about one Case, gathered from /workspace."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import buildlog
from .arch import Arch, lookup
from .package import Package, detect


@dataclass
class Context:
    workspace: Path
    input_dir: Path
    repo: Path
    output_dir: Path
    task: dict
    metadata: dict
    package: Package
    log: buildlog.LogReport
    source_arch: Arch | None
    target_arch: Arch | None

    def as_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "package": self.package.as_dict(),
            "source_arch": self.source_arch.isa if self.source_arch else None,
            "target_arch": self.target_arch.isa if self.target_arch else None,
            "log": self.log.as_dict(),
        }


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _find_key(data: object, names: tuple[str, ...]) -> str | None:
    """Depth-first search for the first string value stored under one of `names`."""
    if isinstance(data, dict):
        for name in names:
            value = data.get(name)
            if isinstance(value, str) and value:
                return value
        for value in data.values():
            found = _find_key(value, names)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_key(value, names)
            if found:
                return found
    return None


def _rebase(path_text: str, workspace: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute() and path.parts[:2] == ("/", "workspace"):
        return workspace.joinpath(*path.parts[2:])
    return path if path.is_absolute() else workspace / path


def _find_log(workspace: Path, input_dir: Path, task: dict) -> Path | None:
    declared = _find_key(task, ("initial_build_log", "target_failure_log", "build_log", "log"))
    candidates = []
    if declared:
        candidates += [_rebase(declared, workspace), input_dir / declared]
    candidates += [input_dir / "initial-build.log", input_dir / "logs" / "target-failure.log"]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    logs = sorted(input_dir.rglob("*.log"), key=lambda p: p.stat().st_size, reverse=True)
    return logs[0] if logs else None


def _arches(meta: dict, log: buildlog.LogReport) -> tuple[Arch | None, Arch | None]:
    source = lookup(_find_key(meta, ("source_arch", "source_architecture")))
    target = lookup(_find_key(meta, ("target_arch", "target_architecture", "architecture")))
    direction = _find_key(meta, ("direction", "migration"))
    if direction and (source is None or target is None):
        match = re.match(r"^\s*([\w-]+?)\s*(?:-to-|->|→|_to_)\s*([\w-]+)\s*$", direction)
        if match:
            source = source or lookup(match.group(1))
            target = target or lookup(match.group(2))
    if target is None:
        target = lookup(log.host_arch)
    return source, target


def load(workspace: Path) -> Context:
    input_dir = workspace / "input"
    repo = workspace / "work" / "repo"
    output_dir = workspace / "output"
    task = _read_json(input_dir / "task.json")
    metadata: dict = {"task": task}
    for extra in sorted(input_dir.glob("*.json")):
        if extra.name != "task.json":
            metadata[extra.stem] = _read_json(extra)
    log = buildlog.analyze(_find_log(workspace, input_dir, task))
    source, target = _arches(metadata, log)
    return Context(
        workspace=workspace,
        input_dir=input_dir,
        repo=repo,
        output_dir=output_dir,
        task=task,
        metadata=metadata,
        package=detect(repo),
        log=log,
        source_arch=source,
        target_arch=target,
    )
