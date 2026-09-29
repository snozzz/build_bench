# build_bench

Our entry for the [Build-Bench Challenge](https://matrix.cstcloud.cn/build-bench/) (ICSE 2027 Competition Track):
an LLM-based Agent that repairs cross-architecture package build failures
(x86_64 ↔ aarch64 ↔ riscv64).

## Layout

```text
infra/wsl/        one-time setup of the WSL build host (Docker, binfmt, proxy tunnel)
scripts/          developer helpers (sync code to the build host, ...)
tools/            dataset analysis and local evaluation tooling
```

## Workflow

The Mac only holds source code. Everything large (datasets, Docker images, build runs)
lives on the WSL build host `snoz@36.151.149.108` under `~/bb/`:

```text
~/bb/build_bench/                  this repo, mirrored by scripts/sync-to-wsl.sh
~/bb/BuildBench-Agent-Baseline/    official Starter Kit (bb doctor/test/check/package)
~/bb/data/dev/                     200 public Development Cases (v0.1)
~/bb/ref/validator/                official validator source (release v0.1.0-rc.3)
~/bb/results/                      analysis output and local runs
```

```bash
./scripts/sync-to-wsl.sh                                  # push code to WSL
ssh snoz@36.151.149.108 'cd ~/bb && python3 build_bench/tools/analyze_cases.py data/dev/buildbench-development-cases-v0.1'
```

## Key facts about the task

- The submission is a managed Python 3.11 Agent (`agent.yaml`, `src/`, `requirements.lock`, `README.md`).
  It runs once per Case with `/workspace/input` (read-only), `/workspace/work/repo` (writable), `/workspace/output`.
- Local runs use `--network none`, 1 CPU, 1 GiB RAM, no Docker socket; protocol v0.1 has no build feedback.
- The canonical repair is a **text-only** diff of `work/repo` (binary changes are rejected), replayed with
  `git apply` and rebuilt by obs-build. Debian cases are therefore edited as an unpacked source tree;
  upstream changes belong in new quilt patches under `debian/patches/`.
- Failure logs can be huge (one dev log is 2.4 GB): always read logs from the tail.
