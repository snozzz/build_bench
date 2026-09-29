# archfix-agent

Repair Agent for the Build-Bench Challenge. It repairs a package that builds on the
source architecture but fails on the target architecture, editing only
`/workspace/work/repo`.

## Pipeline

1. **Context** (`src/context.py`): reads `/workspace/input` (task metadata, failure log),
   resolves source/target architectures, and locates the package in the worktree
   (unpacked Debian tree, packed `.dsc`, or RPM spec).
2. **Log analysis** (`src/buildlog.py`): reads only the log tail (logs can exceed 2 GB),
   isolates the final error cascade, the first concrete errors, and referenced source lines.
3. **Deterministic fixers** (`src/fixers/`): high-confidence repairs such as
   dpkg-gensymbols mismatches (symbols missing on the target are tagged `optional`).
4. **Finalize** (`src/finalize.py`): for Debian `3.0 (quilt)` sources, upstream edits are
   recorded as quilt hunks so `dpkg-source -b` accepts the repaired tree. They are folded
   into the last existing patch when there is one (no new files), otherwise written as a
   new patch appended to the series. Other formats keep direct edits.

All edits go through `ChangeSet` (`src/changes.py`), which confines writes to the package
tree, refuses binary/non-UTF-8 files and files with unusual line breaks (form feeds, lone
CR), preserves CRLF endings, and reverts everything if the Agent fails internally.

## Output

- `/workspace/output/agent-result.json`: protocol v0.1 completion record.
- `/workspace/output/archfix-report.json`: diagnostics (context, log findings, notes).

## Dependencies

Python 3.11 standard library only.
