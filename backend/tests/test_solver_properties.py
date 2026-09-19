"""Property-based tests for the Solver's backtracking solve (task 4.2).

This file implements design.md Property 4 with hypothesis. It is kept separate
from the example-based ``test_solver.py`` (task 4.1/4.4) so the two never
collide — this file holds only property tests.

Feature: xray-sudoku-demo, Property 4
"""

from __future__ import annotations

import multiprocessing
import random

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain.board import (
    SIZE,
    Board,
    is_complete_and_valid,
    is_valid_placement,
)
from backend.domain.solver import Solver
from backend.domain.tracing import FakeTracer

# Upper bound, in seconds, on a single ``solve`` call inside a property example.
# ``solve`` on a valid or a densely contradictory board returns near-instantly;
# this cap only ever trips on inputs the solver cannot resolve in bounded time,
# which for Property 5 is itself a property violation (see the guarded helper).
_SOLVE_TIME_LIMIT_SECONDS = 5.0


def _new_solver() -> Solver:
    """Return a Solver wired to a FakeTracer, so the test stays fully offline."""
    return Solver(FakeTracer())


def _solve_in_subprocess(board: Board, result_queue: multiprocessing.Queue) -> None:  # type: ignore[type-arg]
    """Run ``solve`` in a worker process, putting ``solve(board) is None`` on the queue."""
    result_queue.put(_new_solver().solve(board) is None)


def _solve_returns_none_within_limit(board: Board) -> bool:
    """Whether ``solve(board)`` returns ``None`` within ``_SOLVE_TIME_LIMIT_SECONDS``.

    ``solve`` is run in a worker process so a solver that cannot resolve the
    board in bounded time is terminated and reported as *not* returning ``None``,
    rather than hanging the offline test suite. A solver that genuinely reports
    "no solution" for a contradictory board returns quickly and truthy here.
    """
    result_queue: multiprocessing.Queue = multiprocessing.Queue()  # type: ignore[type-arg]
    worker = multiprocessing.Process(
        target=_solve_in_subprocess, args=(board, result_queue)
    )
    worker.start()
    worker.join(_SOLVE_TIME_LIMIT_SECONDS)
    if worker.is_alive():
        worker.terminate()
        worker.join()
        return False  # did not report a result in time: not a reported "no solution"
    return bool(result_queue.get())


def _agrees_with_givens(solution: Board, givens: Board) -> bool:
    """Whether ``solution`` matches every non-zero (given) cell of ``givens``."""
    for row in range(SIZE):
        for col in range(SIZE):
            given = givens[row][col]
            if given != 0 and solution[row][col] != given:
                return False
    return True


# Feature: xray-sudoku-demo, Property 4
# Validates: Requirements 5.2, 5.3
#
# Guaranteed-solvable boards come from Solver.generate(random.Random(seed)),
# which returns a puzzle with a unique solution; hypothesis drives the seed.
@given(seed=st.integers())
@settings(max_examples=100, deadline=None)
def test_property_backtracking_returns_valid_consistent_completion(seed: int) -> None:
    solver = _new_solver()
    generated = solver.generate(random.Random(seed))

    result = solver.solve(generated.puzzle)

    # A solvable board yields a fully filled, constraint-satisfying board...
    assert result is not None
    assert is_complete_and_valid(result)
    # ...that agrees with every original given cell of the input puzzle...
    assert _agrees_with_givens(result, generated.puzzle)
    # ...and, since the generated puzzle has a unique solution, equals it.
    assert result == generated.solution


# Feature: xray-sudoku-demo, Property 5
# Validates: Requirements 5.4
#
# A board carrying a *direct* placement contradiction — the same value twice in
# one row, column, or box — cannot be completed, so ``solve`` must return None.
# The strategy below builds such a board: it picks a value 1..9, a conflict unit
# (row / column / box), and two distinct cells sharing that unit, and sets both
# to the value on an otherwise-empty board. Varying the unit exercises all three
# constraint kinds across examples.
_ConflictBoard = tuple[Board, tuple[int, int], tuple[int, int]]


def _empty_board() -> Board:
    """Return a fresh all-zero 9x9 board."""
    return [[0] * SIZE for _ in range(SIZE)]


def _box_cells(box_row: int, box_col: int) -> list[tuple[int, int]]:
    """Return the nine (row, col) cells of the 3x3 box at the given box origin."""
    return [
        (box_row * 3 + r, box_col * 3 + c)
        for r in range(3)
        for c in range(3)
    ]


@st.composite
def _boards_with_direct_contradiction(draw: st.DrawFn) -> _ConflictBoard:
    """Draw an otherwise-empty board with one value placed twice in a shared unit.

    Returns the board together with the two conflicting cells, so the test can
    sanity-check that the contradiction it built is real before asserting on it.
    """
    value = draw(st.integers(min_value=1, max_value=SIZE))
    unit = draw(st.sampled_from(("row", "column", "box")))

    if unit == "row":
        row = draw(st.integers(min_value=0, max_value=SIZE - 1))
        col_a, col_b = draw(
            st.lists(
                st.integers(min_value=0, max_value=SIZE - 1),
                min_size=2,
                max_size=2,
                unique=True,
            )
        )
        cell_a, cell_b = (row, col_a), (row, col_b)
    elif unit == "column":
        col = draw(st.integers(min_value=0, max_value=SIZE - 1))
        row_a, row_b = draw(
            st.lists(
                st.integers(min_value=0, max_value=SIZE - 1),
                min_size=2,
                max_size=2,
                unique=True,
            )
        )
        cell_a, cell_b = (row_a, col), (row_b, col)
    else:  # box
        box_row = draw(st.integers(min_value=0, max_value=2))
        box_col = draw(st.integers(min_value=0, max_value=2))
        cells = _box_cells(box_row, box_col)
        indices = draw(
            st.lists(
                st.integers(min_value=0, max_value=len(cells) - 1),
                min_size=2,
                max_size=2,
                unique=True,
            )
        )
        cell_a, cell_b = cells[indices[0]], cells[indices[1]]

    board = _empty_board()
    board[cell_a[0]][cell_a[1]] = value
    board[cell_b[0]][cell_b[1]] = value
    return board, cell_a, cell_b


def _shares_a_unit(cell_a: tuple[int, int], cell_b: tuple[int, int]) -> bool:
    """Whether two cells share a row, a column, or a 3x3 box (a real conflict)."""
    (row_a, col_a), (row_b, col_b) = cell_a, cell_b
    same_row = row_a == row_b
    same_col = col_a == col_b
    same_box = (row_a // 3, col_a // 3) == (row_b // 3, col_b // 3)
    return same_row or same_col or same_box


@given(conflict=_boards_with_direct_contradiction())
@settings(max_examples=100, deadline=None)
def test_property_unsolvable_board_yields_no_solution(
    conflict: _ConflictBoard,
) -> None:
    board, cell_a, cell_b = conflict

    # Guard against a strategy bug: the two duplicated cells must genuinely share
    # a unit, otherwise the board would not actually be contradictory.
    assert _shares_a_unit(cell_a, cell_b)

    # The key assertion: a directly contradictory board has no solution, so
    # solve must report None (within a bounded time — see the helper).
    assert _solve_returns_none_within_limit(board)


def test_solve_returns_none_for_two_fives_in_the_first_row() -> None:
    """Concrete example: two 5s in row 0 make the board unsolvable."""
    board = _empty_board()
    board[0][0] = 5
    board[0][8] = 5

    assert _solve_returns_none_within_limit(board)


# Feature: xray-sudoku-demo, Property 1
# Validates: Requirements 2.2
#
# The uniqueness check below is deliberately independent of the Solver's own
# internal solution counter: it is a self-contained backtracking count written
# against only the public board API (``is_valid_placement``), so asserting on it
# is a real check of the generated puzzle, not a tautology restating solver.py.
def _count_completions(board: Board, limit: int) -> int:
    """Count valid completions of ``board`` in place, stopping once ``limit`` reached.

    A plain first-empty-cell backtracking count over a working copy, using only
    ``is_valid_placement`` from the board API. The ``limit`` cap lets a caller ask
    just "one, or more than one?" — counting to 2 is enough to decide uniqueness
    and keeps the check fast on a ~40-given puzzle.
    """
    working = [row[:] for row in board]
    return _count(working, limit)


def _count(board: Board, limit: int) -> int:
    """Recursively count completions of ``board`` in place, capped at ``limit``."""
    cell = _first_empty_cell(board)
    if cell is None:
        return 1  # no empty cells remain: this filled board is one completion

    row, col = cell
    total = 0
    for value in range(1, SIZE + 1):
        if is_valid_placement(board, row, col, value):
            board[row][col] = value
            total += _count(board, limit - total)
            board[row][col] = 0
            if total >= limit:
                return total
    return total


def _first_empty_cell(board: Board) -> tuple[int, int] | None:
    """Return the first empty cell in row-major order, or ``None`` if full."""
    for row in range(SIZE):
        for col in range(SIZE):
            if board[row][col] == 0:
                return row, col
    return None


@given(seed=st.integers())
@settings(max_examples=100, deadline=None)
def test_property_generated_puzzle_has_unique_solution(seed: int) -> None:
    solver = _new_solver()
    generated = solver.generate(random.Random(seed))

    # Counting completions independently (capped at 2) must find exactly one...
    assert _count_completions(generated.puzzle, limit=2) == 1
    # ...and that single completion must be the solution the generator reported.
    solved = solver.solve(generated.puzzle)
    assert solved == generated.solution


# Feature: xray-sudoku-demo, Property 2
# Validates: Requirements 2.2
#
# The determinism rule in the engineering-practices steering requires that the
# generator take an explicit random generator and that the same seed reproduce
# the same puzzle. This property pins exactly that: equal seeds yield identical
# givens and identical solutions, across repeated calls and across independent
# solver instances (so nothing leaks through hidden shared or module-level
# state). Hypothesis drives the seed.
@given(seed=st.integers())
@settings(max_examples=100, deadline=None)
def test_property_generation_is_reproducible_from_seed(seed: int) -> None:
    # Two independent solvers, each with its own tracer, so a pass proves the
    # output depends only on the injected rng, not on any per-solver or
    # module-level state carried between calls.
    first = _new_solver().generate(random.Random(seed))
    second = _new_solver().generate(random.Random(seed))

    # The same seed must reproduce the same puzzle: identical givens...
    assert first.puzzle == second.puzzle
    # ...and identical solutions.
    assert first.solution == second.solution


def test_different_seeds_produce_different_puzzles() -> None:
    """Concrete example: two specific, distinct seeds give distinct puzzles.

    Kept out of the property above on purpose — distinct seeds *could* collide in
    principle, so "different seed differs" is asserted only for two seeds known
    to differ, not as an invariant over all inputs.
    """
    from_seed_1 = _new_solver().generate(random.Random(1))
    from_seed_2 = _new_solver().generate(random.Random(2))

    assert from_seed_1.puzzle != from_seed_2.puzzle
