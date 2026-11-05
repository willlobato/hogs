# hogs

Single-file CLI (`hogs.py`) for macOS; `rich` is the only dependency. Keep it that way.

## Cleanup safety invariants

`--clean` only offers what is reclaimable. These are guardrails: a change that weakens one breaks the tool's core promise, even if it simplifies the code.

- Docker volumes, running containers and images used by any container are shown as information, never offered for deletion.
- Any host folder bind-mounted by any container (running or stopped) — and anything inside or containing it — is never suggested by file-based blocks. A named volume backed by a host folder (`o` with `bind` + `device` in its options) protects that folder the same way; if those options can't be read, mounts are unknown.
- Mounts unknown (Docker installed, not answering, nothing saved in `mounts.json`) → the blocks that find files by scanning (personal files, big app caches in `~/Library/Caches`) suggest nothing. `PATH_DEFS` items are fixed known paths and are still offered.
- The one volume exception: BuildKit state volumes (`buildx_buildkit_*_state`) are build cache. They are reclaimed only through `docker buildx prune`, never removed as volumes.
- `--docker-gc` prunes with `--max-used-space`, which keeps the most recent cache. Pruning a `docker`-driver builder works only through the current context's builder (`default` fails on Docker Desktop).
- Locally built images (`origin == "local"`) are listed one per item, never grouped, always 🔴. Exception: rollback tags (`rollback_base()` with the current `repo:base` image present) are one item each, 🟢 after `rollback_days` (age = youngest of tag date, current image `Created`, first seen), 🟡 before. The report shows each one on its own row (never folded into a total) and the recent ones are listed by name in "✨ Recomendado agora". Also 🟢 at any age when the current image's OCI `revision` (hex only) is an ancestor of the default branch of a local clone (under `worktrees_root`) whose remote matches the OCI `source`; checked locally, never fetched; anything else falls back to `rollback_days`.
- When `image_origin` is unsure, classify toward the higher risk. Same for a stopped container's age: unknown
  age is never "old" (🟢); a never-started container ages from `Created`.
- Paused, restarting and removing containers count as running (`ACTIVE_STATES`), also for their compose project.
- `docker rmi` targets the exact reference (`img["ref"]`): an untagged `repo` pulled by digest is removed as
  `repo@sha256:…`, never as bare `repo` (= `repo:latest`).
- Cleanup actions raise on failure (`require`/`run_all`), so `clean()` reports ↷ instead of ✔.
- A stopped container whose compose project still has something running stays out of the 🟢 group.
- Every deletion goes through the selection + `Confirm` flow.
- Personal files are only moved to the Trash (Finder, path via `argv`), never deleted; they are always 🟡 `EVAL`, never in `a`; size is allocated blocks (iCloud dataless files never count).
- Worktree deletion uses `git worktree remove` without force and preserves branches. Recheck HEAD,
  branch, merge, activity, ignored/untracked files, process/container usage immediately
  before removal. Unknown checks, non-regenerable ignored files (anything outside `REGENERABLE`), main
  checkouts, detached/locked worktrees and submodules block deletion.
- An unmerged worktree is 🟢 only when untouched for `stale_worktree_days`, HEAD contained in a local
  remote-tracking ref (never fetched), clean, no protected ignored files, nothing running, no live Claude
  Code session citing it and no linked Claude Code conversation within the window. Unpushed commits keep
  it ⚪ at any age; conversations unreadable → ⚪. Merged worktrees ignore conversations.
- Anything running inside a worktree right now (lsof process or cwd, container mount, a Claude Code
  session opened inside it) blocks deletion. Agent sessions that only worked on it (live Claude Code
  transcripts, recent Codex) are shown by name but do not block: the author decided merged + clean is
  enough, since nothing is lost.
- macOS advice (restart, apply update) is text only: never executed, never an `Item`.
- "com o recomendado" and "✨ Recomendado agora" count only 🟢 (`SAFE`) `clean_items`; ↻ advice and 🟡 `EVAL`
  are listed apart and never add to it. `breakdown()` parts never exceed the used space (no double counting).
  With Docker's TRIM off (`DiskTRIM` false), Docker items don't add to "com o recomendado" (the space stays in
  `Docker.raw`) and the header says so.
- App code-sign clones (`*.code_sign_clone`) are offered only for versions different from the installed app's,
  only while the app is closed, sized as one copy per old version; unknown installed version → nothing offered.
- "App open" guards (`process_running`) never miss an open app: the name is regex-escaped and matches by full
  name, by the name cut at `MAXCOMLEN` (16) or by the `…/Contents/MacOS/<exe>` path; any match counts as open.
- hogs only reads `~/.claude` (transcripts, sessions, settings); `cleanupPeriodDays` is a recommendation, never written.

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
