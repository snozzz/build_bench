#!/usr/bin/env python3
"""Local Case harness mirroring the official Agent workflow on public Development Cases.

  prepare  dataset Case -> <run>/workspace/{input,work/repo/input,output} + pristine copy
  agent    run the Agent (host python, or --docker with the official runtime limits)
  diff     canonical repair.diff using the Starter Kit's generator (text-only, input/ prefix)
  pack     git-apply repair.diff to a fresh tree and rebuild the source package (dpkg-source -b)
  case     prepare + agent + diff + pack for one Case
  batch    `case` over many Cases, writing <out>/summary.csv
  validate run the official local validator bundle on a packed run (only Cases with a bundle)
  build    approximate target-arch rebuild (tools/localbuild.py) of runs: repaired and/or
           original source, results in <run>/localbuild-{fix,orig}/result.json

The Starter Kit generator writes created/deleted files without git mode headers, which
`git apply` rejects; the platform documents full git headers, so the harness adds them and
reports `creates_files` so such repairs can be tracked separately.

Environment: BB_STARTER_KIT (default ~/bb/BuildBench-Agent-Baseline/starter-kit).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AGENT_DIR = HERE.parent / "agent"
STARTER_KIT = Path(os.environ.get("BB_STARTER_KIT", "~/bb/BuildBench-Agent-Baseline/starter-kit")).expanduser()
AGENT_IMAGE = os.environ.get("BB_AGENT_IMAGE", "python:3.11.9-slim-bookworm")


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def case_dirs(root: Path) -> list[Path]:
    return sorted(p.parent for p in root.glob("cases/*/*/case.json"))


# -- prepare --------------------------------------------------------------------------------
def prepare(case_dir: Path, run: Path) -> None:
    case = json.loads((case_dir / "case.json").read_text())
    if run.exists():
        shutil.rmtree(run)
    ws = run / "workspace"
    for sub in ("input", "work/repo", "output"):
        (ws / sub).mkdir(parents=True, exist_ok=True)

    dsc = next((case_dir / case["paths"]["source_input"]).glob("*.dsc"))
    tree = ws / "work" / "repo" / "input"
    res = sh(["dpkg-source", "--no-check", "-x", str(dsc), str(tree)])
    if res.returncode != 0:
        raise RuntimeError(f"dpkg-source -x failed: {res.stderr[-2000:]}")

    log_src = case_dir / case["paths"]["target_failure_log"]
    log_dst = ws / "input" / "initial-build.log"
    try:
        os.link(log_src, log_dst)
    except OSError:
        shutil.copy2(log_src, log_dst)
    shutil.copy2(case_dir / "case.json", ws / "input" / "case.json")
    (ws / "input" / "task.json").write_text(json.dumps({
        "schema_version": "0.1",
        "case_id": case["case_id"],
        "worktree": "/workspace/work/repo",
        "initial_build_log": "/workspace/input/initial-build.log",
        "source_arch": case["platform"]["source_arch"],
        "target_arch": case["platform"]["target_arch"],
    }, indent=2) + "\n")

    sh(["cp", "-a", "--reflink=auto", str(ws / "work" / "repo"), str(run / "original")])
    (run / "case-dir").write_text(str(case_dir) + "\n")


# -- agent ----------------------------------------------------------------------------------
def run_agent(run: Path, docker: bool, timeout: int, env_extra: dict[str, str]) -> dict:
    ws = (run / "workspace").resolve()
    started = time.monotonic()
    if docker:
        cmd = [
            "docker", "run", "--rm", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--network", os.environ.get("BB_AGENT_NETWORK", "none"),
            "--cpus", os.environ.get("BB_AGENT_CPUS", "1"),
            "--memory", os.environ.get("BB_AGENT_MEMORY", "1g"),
            "--pids-limit", "128", "--user", f"{os.getuid()}:{os.getgid()}",
            "-e", "HOME=/tmp", "-e", "PYTHONDONTWRITEBYTECODE=1",
            "-e", "BB_WORKSPACE=/workspace", "-e", "PYTHONPATH=/agent",
            "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
            "-v", f"{AGENT_DIR}:/agent:ro",
            "-v", f"{ws / 'input'}:/workspace/input:ro",
            "-v", f"{ws / 'work'}:/workspace/work",
            "-v", f"{ws / 'output'}:/workspace/output",
            "-w", "/agent",
        ]
        for key, value in env_extra.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += [AGENT_IMAGE, "python", "-m", "src.main"]
        env = None
    else:
        cmd = [sys.executable, "-m", "src.main"]
        env = {**os.environ, "BB_WORKSPACE": str(ws), "PYTHONDONTWRITEBYTECODE": "1", **env_extra}
    try:
        res = subprocess.run(cmd, cwd=AGENT_DIR, env=env, text=True, capture_output=True, timeout=timeout)
        code, out = res.returncode, res.stdout + res.stderr
    except subprocess.TimeoutExpired as exc:
        code, out = -9, f"timeout after {timeout}s\n{exc.stdout or ''}{exc.stderr or ''}"
    (run / "agent.log").write_text(out)
    result_path = ws / "output" / "agent-result.json"
    result = json.loads(result_path.read_text()) if result_path.is_file() else {}
    return {"exit": code, "seconds": round(time.monotonic() - started, 1), "result": result}


# -- diff / pack ----------------------------------------------------------------------------
def canonical_diff(run: Path) -> tuple[bool, str]:
    sys.path.insert(0, str(STARTER_KIT))
    from runner.generate_patch import PatchGenerationError, generate_patch  # type: ignore

    try:
        changed = generate_patch(
            run / "original", run / "workspace" / "work" / "repo", run / "repair.diff", "input/"
        )
    except (PatchGenerationError, OSError, UnicodeError) as error:
        (run / "repair.diff").unlink(missing_ok=True)
        return False, str(error)
    text = (run / "repair.diff").read_bytes().decode("utf-8")
    text = re.sub(r"^(diff --git .*\n)(--- /dev/null\n)", r"\1new file mode 100644\n\2", text, flags=re.M)
    text = re.sub(r"^(diff --git .*\n)(--- a/.*\n\+\+\+ /dev/null\n)", r"\1deleted file mode 100644\n\2", text, flags=re.M)
    (run / "repair.diff").write_bytes(text.encode("utf-8"))
    return True, ", ".join(changed)


def creates_files(run: Path) -> bool:
    diff = run / "repair.diff"
    return diff.is_file() and bool(re.search(r"^new file mode", diff.read_text(), re.M))


def pack(run: Path) -> tuple[bool, str]:
    """Replay repair.diff on a fresh tree and rebuild the source package."""
    case_dir = Path((run / "case-dir").read_text().strip())
    case = json.loads((case_dir / "case.json").read_text())
    dsc = next((case_dir / case["paths"]["source_input"]).glob("*.dsc"))
    replay = run / "replay"
    shutil.rmtree(replay, ignore_errors=True)
    replay.mkdir()
    res = sh(["dpkg-source", "--no-check", "-x", str(dsc), str(replay / "input")])
    if res.returncode != 0:
        return False, "extract: " + res.stderr[-500:]
    patch = (run / "repair.diff").resolve()
    res = sh(["git", "apply", "--check", str(patch)], cwd=replay)
    if res.returncode != 0:
        return False, "git apply --check: " + res.stderr[-500:]
    sh(["git", "apply", str(patch)], cwd=replay)
    out = run / "source-package"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    # dpkg-source -b expects the orig tarball(s) next to the tree.
    for f in (case_dir / case["paths"]["source_input"]).iterdir():
        if ".orig" in f.name:
            shutil.copy2(f, replay / f.name)
    res = sh(["dpkg-source", "-b", "input"], cwd=replay)
    if res.returncode != 0:
        return False, "dpkg-source -b: " + (res.stderr or res.stdout)[-800:]
    for f in replay.iterdir():
        if f.is_file():
            shutil.move(str(f), out / f.name)
    shutil.rmtree(replay, ignore_errors=True)
    return True, " ".join(sorted(p.name for p in out.iterdir()))


def validate(run: Path, bundle: Path) -> tuple[bool, str]:
    """Build the packed source with an official local validator bundle (run.sh + case/)."""
    repaired = run / "validator-case"
    shutil.rmtree(repaired, ignore_errors=True)
    shutil.copytree(bundle / "case", repaired, symlinks=True, ignore=shutil.ignore_patterns("input"))
    shutil.copytree(run / "source-package", repaired / "input")
    out = run / "validation"
    shutil.rmtree(out, ignore_errors=True)
    res = sh(["bash", str(bundle / "run.sh"), "--input", str(repaired), "--output", str(out)])
    (run / "validator.console.log").write_text(res.stdout + res.stderr)
    result = out / "build-result.json"
    status = json.loads(result.read_text()).get("status") if result.is_file() else "missing"
    return res.returncode == 0 and status == "succeeded", str(status)


def local_build(run: Path, which: str, jobs: int, memory: str) -> dict:
    import localbuild

    case_dir = Path((run / "case-dir").read_text().strip())
    case = json.loads((case_dir / "case.json").read_text())
    src = run / "source-package" if which == "fix" else case_dir / case["paths"]["source_input"]
    if not src.is_dir():
        return {"status": "no-source"}
    try:
        return localbuild.build(src, case["platform"]["series"], case["platform"]["target_arch"],
                                run / f"localbuild-{which}", jobs=jobs, memory=memory)
    except Exception as error:
        return {"status": "error", "error": repr(error)[:300]}


def one_case(case_dir: Path, run: Path, args) -> dict:
    row = {"case_id": case_dir.name, "direction": case_dir.parent.name}
    try:
        prepare(case_dir, run)
        agent = run_agent(run, args.docker, args.timeout, {})
        row.update(exit=agent["exit"], seconds=agent["seconds"],
                   message=str(agent["result"].get("message", ""))[:300])
        ok, info = canonical_diff(run)
        row.update(diff=ok, diff_info=info[:300], creates_files=creates_files(run))
        if ok:
            ok, info = pack(run)
            row.update(pack=ok, pack_info=info[:300])
    except Exception as error:  # keep the batch going
        row.update(error=repr(error)[:300])
    if not args.keep:
        for sub in ("workspace", "original"):
            shutil.rmtree(run / sub, ignore_errors=True)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("case"); p.add_argument("case_dir", type=Path); p.add_argument("run", type=Path)
    lb = sub.add_parser("build"); lb.add_argument("runs", type=Path)
    lb.add_argument("--filter", default=""); lb.add_argument("--which", choices=("fix", "orig", "both"), default="fix")
    lb.add_argument("--parallel", type=int, default=2); lb.add_argument("--build-jobs", type=int, default=8)
    lb.add_argument("--memory", default="3g")
    v = sub.add_parser("validate"); v.add_argument("run", type=Path); v.add_argument("bundle", type=Path)
    b = sub.add_parser("batch"); b.add_argument("dataset", type=Path); b.add_argument("out", type=Path)
    b.add_argument("--filter", default=""); b.add_argument("--jobs", type=int, default=4)
    for s in (p, b):
        s.add_argument("--docker", action="store_true")
        s.add_argument("--timeout", type=int, default=1800)
        s.add_argument("--keep", action="store_true", help="keep workspace trees")
    args = ap.parse_args()

    if args.cmd == "build":
        runs = sorted(p for p in args.runs.iterdir()
                      if (p / "case-dir").is_file() and re.search(args.filter, p.name)
                      and (args.which == "orig" or (p / "source-package").is_dir()))
        whiches = ["orig", "fix"] if args.which == "both" else [args.which]
        jobs = [(r, w) for r in runs for w in whiches]
        with cf.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = {pool.submit(local_build, r, w, args.build_jobs, args.memory): (r, w) for r, w in jobs}
            for future in cf.as_completed(futures):
                r, w = futures[future]
                res = future.result()
                print(f"{r.name} [{w}]: {res.get('status')} {res.get('seconds', '')}s", flush=True)
        return 0
    if args.cmd == "validate":
        ok, status = validate(args.run.resolve(), args.bundle.resolve())
        print(json.dumps({"succeeded": ok, "status": status}))
        return 0 if ok else 1
    if args.cmd == "case":
        args.keep = True
        print(json.dumps(one_case(args.case_dir.resolve(), args.run.resolve(), args), indent=2))
        return 0

    cases = [c for c in case_dirs(args.dataset) if re.search(args.filter, c.name)]
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    with cf.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(one_case, c, (args.out / c.name).resolve(), args): c for c in cases}
        for future in cf.as_completed(futures):
            row = future.result()
            rows.append(row)
            print(f"[{len(rows)}/{len(cases)}] {row['case_id']}: diff={row.get('diff')} pack={row.get('pack')} {row.get('message', row.get('error', ''))[:120]}", flush=True)
    rows.sort(key=lambda r: r["case_id"])
    fields = ["case_id", "direction", "exit", "seconds", "diff", "creates_files", "pack", "message", "diff_info", "pack_info", "error"]
    with (args.out / "summary.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"changed: {sum(1 for r in rows if r.get('diff'))}/{len(rows)}  packed: {sum(1 for r in rows if r.get('pack'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
