# hogs

Single-file CLI (`hogs.py`) for macOS; `rich` is the only dependency. Keep it that way.

## Cleanup safety invariants

`--clean` only offers what is reclaimable. These are guardrails: a change that weakens one breaks the tool's core promise, even if it simplifies the code.

- Docker volumes, running containers and images used by any container are shown as information, never offered for deletion.
- Locally built images (`origin == "local"`) are listed one per item, never grouped, always 🔴.
- When `image_origin` is unsure, classify toward the higher risk.
- A stopped container whose compose project still has something running stays out of the 🟢 group.
- Every deletion goes through the selection + `Confirm` flow.

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
