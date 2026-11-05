# hogs

Single-file CLI (`hogs.py`) for macOS; `rich` is the only dependency. Keep it that way.

## Cleanup safety invariants

`--clean` only offers what is reclaimable. These are guardrails: a change that weakens one breaks the tool's core promise, even if it simplifies the code.

- Docker volumes, running containers and images used by any container are shown as information, never offered for deletion.
- Any host folder bind-mounted by any container (running or stopped) — and anything inside or containing it — is never suggested by file-based blocks. Mounts unknown (Docker installed, not answering, nothing saved in `mounts.json`) → no file suggestions at all.
- The one volume exception: BuildKit state volumes (`buildx_buildkit_*_state`) are build cache. They are reclaimed only through `docker buildx prune`, never removed as volumes.
- `--docker-gc` prunes with `--max-used-space`, which keeps the most recent cache. Pruning a `docker`-driver builder works only through the current context's builder (`default` fails on Docker Desktop).
- Locally built images (`origin == "local"`) are listed one per item, never grouped, always 🔴.
- When `image_origin` is unsure, classify toward the higher risk. Same for a stopped container's age: unknown
  age is never "old" (🟢); a never-started container ages from `Created`.
- Paused, restarting and removing containers count as running (`ACTIVE_STATES`), also for their compose project.
- `docker rmi` targets the exact reference (`img["ref"]`): an untagged `repo` pulled by digest is removed as
  `repo@sha256:…`, never as bare `repo` (= `repo:latest`).
- Cleanup actions raise on failure (`require`/`run_all`), so `clean()` reports ↷ instead of ✔.
- A stopped container whose compose project still has something running stays out of the 🟢 group.
- Every deletion goes through the selection + `Confirm` flow.
- Worktree deletion uses `git worktree remove` without force and preserves branches. Recheck HEAD,
  branch, merge, activity, ignored/untracked files, process/container usage immediately
  before removal. Unknown checks, non-regenerable ignored files (anything outside `REGENERABLE`), main
  checkouts, detached/locked worktrees and submodules block deletion.
- Anything running inside a worktree right now (lsof process or cwd, container mount, a Claude Code
  session opened inside it) blocks deletion. Agent sessions that only worked on it (live Claude Code
  transcripts, recent Codex) are shown by name but do not block: the author decided merged + clean is
  enough, since nothing is lost.

New invariants get a test in `tests/test_hogs.py` (see `DockerCleanItemsTest`).

## Conventions

- UI text in Portuguese (pt-BR), sizes with decimal comma via `fmt()`; CLI flags in English.
- Python 3.9 compatible (`from __future__ import annotations`; no `match`, no 3.10+ stdlib APIs). CI runs 3.9 and 3.12.
- New cache/app → one `PathDef` line in `PATH_DEFS`.
- README screenshots come only from `docs/make_screenshots.py`, which uses fictitious data. Real terminal output contains the author's work image names; it stays out of the repo.

## Installation

Three install paths exist (README › Instalação); all of them assume the code is the single file `hogs.py`:

- `install.sh` downloads only `hogs.py` from the `main` branch raw URL, so `main` must always hold a working `hogs.py`.
- `pyproject.toml` ships `py-modules = ["hogs"]` with the entry point `hogs:main`.

Splitting or renaming `hogs.py` means updating `install.sh` and `pyproject.toml` in the same commit.

The version lives in two places, `__version__` in `hogs.py` and `version` in `pyproject.toml`; bump both together.

## Before committing

1. `python3 -m unittest discover -s tests`
2. Visual change → `python3 docs/make_screenshots.py` and commit the SVGs.
3. Behaviour change → update `README.md` (usage table, guarantees).
