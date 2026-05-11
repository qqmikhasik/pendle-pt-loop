# CLAUDE.md — conventions for this repository

This file is read at the start of every Claude Code session in this repo.

## Project type
Python research codebase. `fractal-defi` is the obligatory backtest framework
(per Project 2 spec). Do not introduce alternative backtest libraries.

## Working directory note (Windows / Git Bash)
This repo lives at `C:\C++\pendle-pt-loop\` (POSIX: `/c/C++/pendle-pt-loop/`).
Working directory may silently reset between Bash tool calls; use absolute
paths or `cd /c/C++/pendle-pt-loop && ...` for git-touching commands.

## Style
- Type hints everywhere. Mypy-clean is a goal, not a hard gate.
- `dataclass(frozen=False)` for `GlobalState` / `InternalState` (mutable — entities update in place).
- Module docstrings on every file explaining the protocol mechanic and the modeling choice.
- Functions: ≤30 lines preferred; if longer, extract helpers.
- Tests live in `tests/`, mirror the `src/` tree (`tests/entities/test_pendle_pt.py` for `src/.../entities/pendle_pt.py`).

## fractal-defi integration rules
- Concrete entities extend `BaseEntity[GS, IS]` or a base lending/perp/spot class.
- One `GlobalState` and one `InternalState` per entity, both dataclasses inheriting from the fractal base classes.
- `_initialize_states()` must populate both; never construct the entity without states ready.
- Action methods are named `action_<verb>`; they mutate `_internal_state` only.
- `update_state(state)` runs before `predict` in the strategy loop — put accruals here.
- `balance` property returns equity in the notional unit (USDC for us).
- Strategies extend `BaseStrategy[Params]` with a `Params` dataclass inheriting `BaseStrategyParams`.

## Math conventions
- Prices in USDC unless stated otherwise.
- Rates in annualized decimal (`0.14` = 14% APY).
- Time in years for math, in seconds in `Observation.timestamp`.
- LTV (loan-to-value) = debt / collateral (both in USDC equivalent).
- Health factor = liquidation_LTV / current_LTV (>1 = safe; ≤1 = liquidatable).

## Multi-session walkthrough
Live notes per session live in `/c/C++/Block_Chain_2_solution/Project2_PendleLoop_Walkthrough.md`.
That doc is NOT part of this repo (it's the student's personal reference).
Append a session log there at the end of every working session.

## Memory references for cross-session continuity
- `~/.claude/projects/.../memory/project_blockchain2_project2.md` — project status
- `~/.claude/projects/.../memory/project_hw8_pendle_eda.md` — HW8 Pendle reference data
- `~/.claude/projects/.../memory/project_dex_aggregator.md` — Project 1 patterns to reuse

## Git
- Default branch: `main`.
- Never push without explicit user request.
- Never commit without explicit user request.
- Conventional Commits style (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
- Co-authored-by line per session.

## Things NOT to do
- Do not invent base classes if a fractal-defi base already exists for the concept.
- Do not duplicate fractal-defi loaders — extend or compose them.
- Do not commit data caches (`data/cache/`) or `.env`.
- Do not push to GitHub without user approval (per Project 1 walkthrough convention).
