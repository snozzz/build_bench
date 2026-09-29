#!/usr/bin/env python3
"""Approximate Launchpad-style Debian build in a target-architecture container.

Public Development Cases ship without frozen build environments, so this rebuilds a source
package against the current Ubuntu archive of the Case's series (EOL series come from an
old-releases mirror). Results only approximate the official Validator: dependency versions
can differ from the ones Launchpad used when the failure was recorded.

  localbuild.py SRC_DIR --series noble --arch arm64 --out OUT [--jobs 8] [--timeout 7200]

SRC_DIR holds one .dsc and every file it references. Base images with apt configured and
build-essential installed are cached as bb-build:<series>-<arch>.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

CURRENT_MIRROR = {
    "amd64": "http://mirrors.tuna.tsinghua.edu.cn/ubuntu",
    "arm64": "http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports",
    "riscv64": "http://mirrors.tuna.tsinghua.edu.cn/ubuntu-ports",
}
OLD_MIRROR = "http://mirrors.ustc.edu.cn/ubuntu-old-releases/ubuntu"
CURRENT_SERIES = {"focal", "jammy", "noble", "plucky", "questing", "resolute"}
PLATFORM = {"amd64": "linux/amd64", "arm64": "linux/arm64", "riscv64": "linux/riscv64"}
DEB_ARCH = {"x86_64": "amd64", "aarch64": "arm64", "riscv64": "riscv64"}

BASE_SETUP = r"""
set -e
. /etc/os-release
rm -f /etc/apt/sources.list.d/ubuntu.sources
printf 'deb %s %s main restricted universe multiverse\n' "$MIRROR" "$SERIES" "$MIRROR" "$SERIES-updates" "$MIRROR" "$SERIES-security" > /etc/apt/sources.list
echo 'Acquire::Retries "5";' > /etc/apt/apt.conf.d/80retries
echo 'APT::Install-Recommends "false";' > /etc/apt/apt.conf.d/80norecommends
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q build-essential fakeroot dpkg-dev
useradd -m -u 2000 builder || true
"""

BUILD = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -q >/dev/null
mkdir -p /build && cp -a /src/. /build/ && chown -R builder /build
cd /build
DSC=$(ls *.dsc | head -1)
echo "== build-dep ($BUILD_TYPE) $DSC"
if [ "$BUILD_TYPE" = any ]; then ARCH_ONLY=--arch-only; else ARCH_ONLY=; fi
apt-get build-dep -y -q $ARCH_ONLY "./$DSC" || { echo "BB-STATUS: dep-wait"; exit 3; }
su builder -c "dpkg-source -x --no-check $DSC tree" >/dev/null
cd tree
if [ "$BUILD_TYPE" = any ]; then FLAG=-B; else FLAG=-b; fi
echo "== dpkg-buildpackage $FLAG"
set +e
su builder -c "DEB_BUILD_OPTIONS='parallel=$JOBS' dpkg-buildpackage -us -uc $FLAG"
rc=$?
set -e
ls /build/*.deb 2>/dev/null | xargs -r -n1 basename > /out/debs.txt || true
echo "BB-STATUS: rc=$rc"
exit $rc
"""


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


_image_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def base_image(series: str, arch: str) -> str:
    tag = f"bb-build:{series}-{arch}"
    with _locks_guard:
        lock = _image_locks.setdefault(tag, threading.Lock())
    with lock:
        if sh(["docker", "image", "inspect", tag]).returncode == 0:
            return tag
        return _create_base_image(tag, series, arch)


def _create_base_image(tag: str, series: str, arch: str) -> str:
    mirror = CURRENT_MIRROR[arch] if series in CURRENT_SERIES else OLD_MIRROR
    name = f"bb-base-{series}-{arch}-{uuid.uuid4().hex[:8]}"
    res = sh([
        "docker", "run", "--name", name, "--platform", PLATFORM[arch],
        "-e", f"MIRROR={mirror}", "-e", f"SERIES={series}",
        f"ubuntu:{series}", "bash", "-c", BASE_SETUP,
    ])
    try:
        if res.returncode != 0:
            raise RuntimeError(f"base image setup failed for {tag}:\n{(res.stdout + res.stderr)[-3000:]}")
        commit = sh(["docker", "commit", name, tag])
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr)
    finally:
        sh(["docker", "rm", "-f", name])
    return tag


def build(src: Path, series: str, arch: str, out: Path, jobs: int = 8, timeout: int = 7200,
          memory: str = "6g") -> dict:
    arch = DEB_ARCH.get(arch, arch)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    image = base_image(series, arch)
    build_type = "full" if arch == "amd64" else "any"
    name = f"bb-build-{uuid.uuid4().hex[:12]}"
    cmd = [
        "docker", "run", "--rm", "--name", name, "--platform", PLATFORM[arch],
        "--memory", memory, "--cpus", str(jobs),
        "-e", f"JOBS={jobs}", "-e", f"BUILD_TYPE={build_type}",
        "-v", f"{src.resolve()}:/src:ro", "-v", f"{out.resolve()}:/out",
        image, "bash", "-c", BUILD,
    ]
    started = time.monotonic()
    try:
        with (out / "build.log").open("w") as log:
            res = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        code = res.returncode
    except subprocess.TimeoutExpired:
        sh(["docker", "rm", "-f", name])
        code = -9
    status = {0: "succeeded", 3: "dep-wait", -9: "timeout"}.get(code, "failed")
    debs = (out / "debs.txt").read_text().split() if (out / "debs.txt").is_file() else []
    result = {
        "status": status if status != "succeeded" or debs else "no-artifacts",
        "exit_code": code,
        "seconds": round(time.monotonic() - started),
        "series": series,
        "arch": arch,
        "debs": debs,
    }
    (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("--series", required=True)
    ap.add_argument("--arch", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--jobs", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=7200)
    ap.add_argument("--memory", default="6g")
    args = ap.parse_args()
    result = build(args.src, args.series, args.arch, args.out, args.jobs, args.timeout, args.memory)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
