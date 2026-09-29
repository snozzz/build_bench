#!/bin/bash
# Mirror this repo (code only) to the WSL build host. All builds and tests run there.
set -euo pipefail
WSL_HOST="${WSL_HOST:-snoz@36.151.149.108}"
WSL_DIR="${WSL_DIR:-bb/build_bench}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
rsync -az --delete --exclude .git --exclude-from "$ROOT/.gitignore" "$ROOT/" "$WSL_HOST:$WSL_DIR/"
