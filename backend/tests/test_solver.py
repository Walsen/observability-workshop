"""Example-based tests for the Solver's solve/check_solution (task 4.1).

These pin the concrete behavior of the backtracking solver and the
solved-board check, plus the two X-Ray subsegment names those operations open
via the injected tracer (``solve-backtracking`` and ``check-solution``).

The property tests (Property 4 unsolvable/solved completion via 4.2, Property 5
via 4.3) and the four-subsegment-names unit test (4.7) are separate later tasks;
test names here are prefixed ``test_solver_example_*`` to leave room for them.
Board fixtures are kept local to this file so they never collide with
``test_board.py``.
"""

import random
from collections.abc import Callable

from backend.domain.board import Board, Move, is_complete_and_valid, is_valid_placement
from backend.domain.solver import GeneratedPuzzle, Solver
from backend.domain.tracing import FakeTracer

# A known-good, fully solved 9x9 board (every row, column, and box holds 1..9).
_SOLVED: Board = [
    [5, 3, 4, 6, 7, 8, 9, 1, 2],
    [6, 7, 2, 1, 9, 5, 3, 4, 8],
    [1, 9, 8, 3, 4, 2, 5, 6, 7],
    [8, 5, 9, 7, 6, 1, 4, 2, 3],
    [4, 2, 6, 8, 5, 3, 7, 9, 1],
    [7, 1, 3, 9, 2, 4, 8, 5, 6],
    [9, 6, 1, 5, 3, 7, 2, 8, 4],
    [2, 8, 7, 4, 1, 9, 6, 3, 5],
    [3, 4, 5, 2, 8, 6, 1, 7, 9],
]

# A well-known solvable puzzle with a unique solution (its givens agree with
# _SOLVED — this is the standard "first" Sudoku example).
_PUZZLE: Board = [
    [5, 3, 0, 0, 7, 0, 0, 0, 0],
    [6, 0, 0, 1, 9, 5, 0, 0, 0],
    [0, 9, 8, 0, 0, 0, 0, 6, 0],
    [8, 0, 0, 0, 6, 0, 0, 0, 3],
    [4, 0, 0, 8, 0, 3, 0, 0, 1],
    [7, 0, 0, 0, 2, 0, 0, 0, 6],
    [0, 6, 0, 0, 0, 0, 2, 8, 0],
    [0, 0, 0, 4, 1, 9, 0, 0, 5],
    [0, 0, 0, 0, 8, 0, 0, 7, 9],
]


def _copy(board: Board) -> Board:
    return [row[:] for row in board]


def _new_solver() -> tuple[Solver, FakeTracer]:
    tracer = FakeTracer()
    return Solver(tracer), tracer


def _agrees_with_givens(solution: Board, givens: Board) -> bool:
    for row in range(9):
        for col in range(9):
            given = givens[row][col]
            if given != 0 and solution[row][col] != given:
                return False
    return True


def test_solver_example_solve_completes_nearly_finished_board() -> None:
    solver, _ = _new_solver()
    board = _copy(_SOLVED)
    board[4][4] = 0  # remove a single cell; exactly one completion exists

    result = solver.solve(board)

    assert result == _SOLVED


def test_solver_example_solve_returns_full_valid_solution_for_puzzle() -> None:
    solver, _ = _new_solver()

    result = solver.solve(_PUZZLE)

    assert result is not None
    assert is_complete_and_valid(result)
    assert _agrees_with_givens(result, _PUZZLE)


def test_solver_example_solve_does_not_mutate_the_caller_board() -> None:
    solver, _ = _new_solver()
    board = _copy(_PUZZLE)

    solver.solve(board)

    assert board == _PUZZLE  # caller's board untouched


def test_solver_example_solve_returns_none_on_contradictory_board() -> None:
    solver, _ = _new_solver()
    board = _copy(_PUZZLE)
    # Force a direct contradiction: two 5s in row 0 (5 already sits at [0][0]).
    board[0][1] = 5
    assert not is_valid_placement(board, 0, 0, 5)  # sanity: it is a conflict now

    assert solver.solve(board) is None


def test_solver_example_check_solution_true_on_solved_board() -> None:
    solver, _ = _new_solver()
    assert solver.check_solution(_SOLVED) is True


def test_solver_example_check_solution_false_when_incomplete() -> None:
    solver, _ = _new_solver()
    board = _copy(_SOLVED)
    board[0][0] = 0
    assert solver.check_solution(board) is False


def test_solver_example_solve_opens_solve_backtracking_subsegment() -> None:
    solver, tracer = _new_solver()

    solver.solve(_copy(_PUZZLE))

    assert "solve-backtracking" in tracer.entered


def test_solver_example_check_solution_opens_check_solution_subsegment() -> None:
    solver, tracer = _new_solver()

    solver.check_solution(_SOLVED)

    assert "check-solution" in tracer.entered


# --- generate (task 4.4) ---------------------------------------------------
#
# The exhaustive unique-solution property (Property 1, task 4.5) and the seeded
# reproducibility property (Property 2, task 4.6) are separate later tasks; the
# checks here are small concrete cases that leave those properties for their own
# tasks.


def test_solver_example_generate_returns_a_complete_valid_solution() -> None:
    solver, _ = _new_solver()

    result = solver.generate(random.Random(1234))

    assert isinstance(result, GeneratedPuzzle)
    assert is_complete_and_valid(result.solution)


def test_solver_example_generate_puzzle_givens_agree_with_solution() -> None:
    solver, _ = _new_solver()

    result = solver.generate(random.Random(1234))

    # Every given cell of the puzzle matches the solution; empty cells are 0.
    assert _agrees_with_givens(result.solution, result.puzzle)


def test_solver_example_generate_puzzle_has_empty_cells_but_solution_full() -> None:
    solver, _ = _new_solver()

    result = solver.generate(random.Random(1234))

    givens = sum(1 for row in result.puzzle for cell in row if cell != 0)
    assert 0 < givens < 81  # some cells are blanked; it is not a full board
    assert all(cell != 0 for row in result.solution for cell in row)


def test_solver_example_generate_is_reproducible_from_equal_seeds() -> None:
    solver, _ = _new_solver()

    first = solver.generate(random.Random(2024))
    second = solver.generate(random.Random(2024))

    assert first.puzzle == second.puzzle
    assert first.solution == second.solution


def test_solver_example_generate_opens_generate_subsegment() -> None:
    solver, tracer = _new_solver()

    solver.generate(random.Random(1))

    assert "generate" in tracer.entered


# --- validate_move (task 4.4) ----------------------------------------------


def test_solver_example_validate_move_true_for_legal_placement() -> None:
    solver, _ = _new_solver()
    board = _copy(_PUZZLE)  # cell [0][2] is empty; 4 breaks no rule there

    assert solver.validate_move(board, Move(0, 2, 4)) is True


def test_solver_example_validate_move_false_on_row_conflict() -> None:
    solver, _ = _new_solver()
    board = _copy(_PUZZLE)  # a 5 already sits at [0][0]

    assert solver.validate_move(board, Move(0, 2, 5)) is False


def test_solver_example_validate_move_false_on_column_conflict() -> None:
    solver, _ = _new_solver()
    board = _copy(_PUZZLE)  # a 6 already sits at [1][0]

    assert solver.validate_move(board, Move(2, 0, 6)) is False


def test_solver_example_validate_move_false_on_box_conflict() -> None:
    solver, _ = _new_solver()
    board = _copy(_PUZZLE)  # a 5 sits at [0][0], same top-left box as [2][2]

    assert solver.validate_move(board, Move(2, 2, 5)) is False


def test_solver_example_validate_move_false_onto_filled_cell() -> None:
    solver, _ = _new_solver()
    board = _copy(_PUZZLE)  # [0][0] already holds 5; players fill empty cells

    assert solver.validate_move(board, Move(0, 0, 5)) is False


def test_solver_example_validate_move_opens_validate_subsegment() -> None:
    solver, tracer = _new_solver()

    solver.validate_move(_copy(_PUZZLE), Move(0, 2, 4))

    assert "validate" in tracer.entered


# --- the four named subsegments (task 4.7) ---------------------------------
#
# The example tests above each assert one operation opens its own subsegment.
# This focused test pins all four exact, byte-for-byte names in one place, so a
# rename of any subsegment in solver.py (e.g. "solve" for "solve-backtracking")
# fails here. Each operation runs on its own FakeTracer and is expected to open
# exactly its one subsegment; the inputs are chosen so each traced body runs.


def test_solver_opens_the_four_named_subsegments() -> None:
    # Map each operation to the exact subsegment name it must open. Each lambda
    # runs the operation on a fresh solver bound to the given tracer.
    operations: list[tuple[str, Callable[[Solver], object]]] = [
        ("validate", lambda solver: solver.validate_move(_copy(_PUZZLE), Move(0, 2, 4))),
        ("generate", lambda solver: solver.generate(random.Random(7))),
        ("solve-backtracking", lambda solver: solver.solve(_copy(_PUZZLE))),
        ("check-solution", lambda solver: solver.check_solution(_SOLVED)),
    ]

    for expected_name, run in operations:
        tracer = FakeTracer()
        run(Solver(tracer))
        # Byte-for-byte: exactly this one subsegment opened, and it closed.
        assert tracer.entered == [expected_name]
        assert tracer.exited == [expected_name]
