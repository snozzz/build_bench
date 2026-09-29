"""Architecture naming across ISA, Debian, GNU triplet, and RPM conventions."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Arch:
    isa: str  # x86_64 / aarch64 / riscv64 (competition naming, also RPM)
    deb: str  # amd64 / arm64 / riscv64
    triplet: str  # GNU multiarch triplet
    gcc_macro: str  # predefined compiler macro identifying the ISA


ARCHES = {
    "x86_64": Arch("x86_64", "amd64", "x86_64-linux-gnu", "__x86_64__"),
    "aarch64": Arch("aarch64", "arm64", "aarch64-linux-gnu", "__aarch64__"),
    "riscv64": Arch("riscv64", "riscv64", "riscv64-linux-gnu", "__riscv"),
}

_ALIASES = {
    "x86_64": "x86_64", "amd64": "x86_64", "x86-64": "x86_64", "x64": "x86_64",
    "aarch64": "aarch64", "arm64": "aarch64", "armv8": "aarch64",
    "riscv64": "riscv64", "riscv": "riscv64", "rv64": "riscv64",
}


def lookup(name: str | None) -> Arch | None:
    if not name:
        return None
    key = _ALIASES.get(name.strip().lower())
    return ARCHES.get(key) if key else None
