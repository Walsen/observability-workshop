"""Property-based tests for the pure Sudoku board domain.

Feature: xray-sudoku-demo, Property 3

These use hypothesis to assert placement-rule invariants across arbitrary 9x9
boards. Boards here need not be legal Sudoku boards: arbitrary cell contents in
0..9 are exactly what exercises the placement predicate hard. Example-based
tests for the same module live in ``test_board.py`` (names prefixed
``test_board_example_*``).
"""

from __future__ import annotations

import copy

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain.board import SIZE, Board, is_complete_and_valid, is_valid_placement

# A cell holds 0 (empty) or a digit 1..9.
_cells = st.lists(
    st.lists(st.integers(min_value=0, max_value=9), min_size=SIZE, max_size=SIZE),
    min_size=SIZE,
    max_size=SIZE,
)
_indices = st.integers(min_value=0, max_value=SIZE - 1)
_values = st.integers(min_value=1, max_value=9)


def _box_origin(index: int) -> int:
    return (index // 3) * 3


def _reference_valid(board: Board, row: int, col: int, value: int) -> bool:
    """Independent oracle: True iff ``value`` occurs in no other cell of the
    target's row, column, or 3x3 box.

    This deliberately re-derives the answer from first principles rather than
    reusing the module's helpers, so it can disagree with the implementation.
    """
    for c in range(SIZE):
        if c != col and board[row][c] == value:
            return False
    for r in range(SIZE):
        if r != row and board[r][col] == value:
            return False
    row_start = _box_origin(row)
    col_start = _box_origin(col)
    for r in range(row_start, row_start + 3):
        for c in range(col_start, col_start + 3):
            if (r, c) != (row, col) and board[r][c] == value:
                return False
    return True


@given(board=_cells, row=_indices, col=_indices, value=_values)
@settings(max_examples=200)
def test_property_placement_valid_iff_no_conflict(
    board: Board, row: int, col: int, value: int
) -> None:
    """Property 3: Move validity equals the absence of a placement conflict.

    Validates: Requirements 4.2, 4.3.

    ``is_valid_placement`` returns True if and only if the value does not
    already occur elsewhere in the target cell's row, column, or 3x3 box, and
    calling it never mutates the board (it is a pure predicate).
    """
    before = copy.deepcopy(board)

    result = is_valid_placement(board, row, col, value)

    # Validity matches the independent reference check.
    assert result == _reference_valid(board, row, col, value)

    # The predicate leaves the board unchanged.
    assert board == before


# Feature: xray-sudoku-demo, Property 6

# A completely filled board: every cell holds a digit 1..9, no zeros. These are
# arbitrary filled grids, not necessarily legal Sudoku solutions.
_filled_boards = st.lists(
    st.lists(st.integers(min_value=1, max_value=9), min_size=SIZE, max_size=SIZE),
    min_size=SIZE,
    max_size=SIZE,
)


def _reference_complete_and_valid(board: Board) -> bool:
    """Independent oracle: True iff every cell is filled 1..9 and every row,
    column, and 3x3 box contains each of 1..9 exactly once.

    Re-derived from first principles so it can disagree with the module's
    implementation rather than mirror its internals.
    """
    digits = set(range(1, SIZE + 1))

    for row in range(SIZE):
        for col in range(SIZE):
            if not 1 <= board[row][col] <= 9:
                return False

    for row in range(SIZE):
        if {board[row][col] for col in range(SIZE)} != digits:
            return False

    for col in range(SIZE):
        if {board[row][col] for row in range(SIZE)} != digits:
            return False

    for box_row in range(0, SIZE, 3):
        for box_col in range(0, SIZE, 3):
            box = {
                board[box_row + r][box_col + c] for r in range(3) for c in range(3)
            }
            if box != digits:
                return False

    return True


# A known-valid completed Sudoku board, used for the concrete True/False seeds.
_SOLVED_BOARD: Board = [
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


@given(board=_filled_boards)
@settings(max_examples=200)
def test_property_solved_detection_matches_full_board_validity(board: Board) -> None:
    """Property 6: Solved detection matches full-board validity.

    Validates: Requirements 4.5.

    For any completely filled board, ``is_complete_and_valid`` returns True if
    and only if every row, column, and 3x3 box contains the digits 1..9 exactly
    once, as computed by an independent reference oracle.
    """
    assert is_complete_and_valid(board) == _reference_complete_and_valid(board)


def test_board_example_solved_board_is_complete_and_valid() -> None:
    """A known-valid completed board is detected as solved.

    Validates: Requirements 4.5.
    """
    assert is_complete_and_valid(_SOLVED_BOARD) is True
    assert _reference_complete_and_valid(_SOLVED_BOARD) is True


def test_board_example_duplicate_in_row_breaks_solved_detection() -> None:
    """A completed board with a duplicated digit in a row is not solved.

    Validates: Requirements 4.5.

    Overwriting one cell so its row holds a repeated digit (and is missing
    another) must make detection return False.
    """
    broken = copy.deepcopy(_SOLVED_BOARD)
    # Row 0 is [5, 3, 4, ...]; force the second cell to duplicate the first.
    broken[0][1] = broken[0][0]
    assert is_complete_and_valid(broken) is False
    assert _reference_complete_and_valid(broken) is False
