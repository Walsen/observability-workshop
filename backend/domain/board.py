"""Pure Sudoku board representation and placement rules.

This module is pure domain logic: no ``boto3``, no ``aws_cdk``, no X-Ray, no
I/O. It defines the 9x9 board type, the :class:`Move` value type, and the two
placement-rule predicates the Solver and handlers build on.

Iteration over cells, rows, columns, and boxes uses a defined ``range``-based
order throughout (no set iteration, no reliance on dict ordering), so behavior
is deterministic and reproducible as the engineering-practices steering
requires.
"""

from __future__ import annotations

from dataclasses import dataclass

# A board is a 9x9 grid of ints: 0 marks an empty cell, 1..9 a filled one.
Board = list[list[int]]

SIZE = 9
BOX_SIZE = 3
_DIGITS = frozenset(range(1, SIZE + 1))


@dataclass(frozen=True)
class Move:
    """A single value placement into one cell of the board.

    ``row`` and ``col`` are 0-based indices in ``range(9)``; ``value`` is the
    digit 1..9 being placed.
    """

    row: int
    col: int
    value: int


def _box_origin(index: int) -> int:
    """Return the top or left index of the 3x3 box containing ``index``."""
    return (index // BOX_SIZE) * BOX_SIZE


def _conflicts_in_row(board: Board, row: int, col: int, value: int) -> bool:
    """Whether ``value`` already occupies another cell in ``row``."""
    for c in range(SIZE):
        if c != col and board[row][c] == value:
            return True
    return False


def _conflicts_in_column(board: Board, row: int, col: int, value: int) -> bool:
    """Whether ``value`` already occupies another cell in ``col``."""
    for r in range(SIZE):
        if r != row and board[r][col] == value:
            return True
    return False


def _conflicts_in_box(board: Board, row: int, col: int, value: int) -> bool:
    """Whether ``value`` already occupies another cell in the enclosing box."""
    row_start = _box_origin(row)
    col_start = _box_origin(col)
    for r in range(row_start, row_start + BOX_SIZE):
        for c in range(col_start, col_start + BOX_SIZE):
            if (r, c) != (row, col) and board[r][c] == value:
                return True
    return False


def is_valid_placement(board: Board, row: int, col: int, value: int) -> bool:
    """Whether placing ``value`` at ``(row, col)`` breaks no placement rule.

    Returns ``True`` when ``value`` would not duplicate within its row, its
    column, or its 3x3 box. The target cell itself is never counted as a
    conflict, so re-asserting the value a cell already holds is valid.
    """
    return not (
        _conflicts_in_row(board, row, col, value)
        or _conflicts_in_column(board, row, col, value)
        or _conflicts_in_box(board, row, col, value)
    )


def _group_holds_all_digits(values: list[int]) -> bool:
    """Whether ``values`` is exactly the nine digits 1..9 with no duplicates."""
    return len(values) == SIZE and set(values) == _DIGITS


def is_complete_and_valid(board: Board) -> bool:
    """Whether ``board`` is fully filled and satisfies every Sudoku constraint.

    Returns ``True`` only when there are no empty cells and every row, column,
    and 3x3 box contains the digits 1..9 exactly once.
    """
    for row in range(SIZE):
        if not _group_holds_all_digits([board[row][col] for col in range(SIZE)]):
            return False

    for col in range(SIZE):
        if not _group_holds_all_digits([board[row][col] for row in range(SIZE)]):
            return False

    for box_row in range(0, SIZE, BOX_SIZE):
        for box_col in range(0, SIZE, BOX_SIZE):
            cells = [
                board[box_row + r][box_col + c]
                for r in range(BOX_SIZE)
                for c in range(BOX_SIZE)
            ]
            if not _group_holds_all_digits(cells):
                return False

    return True
