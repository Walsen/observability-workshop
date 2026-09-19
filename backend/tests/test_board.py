"""Example-based tests for the pure Sudoku board domain (task 2.1).

These pin the core behavior of the placement rules with concrete cases. The
property tests (Property 3, Property 6) and the fuller edge-case unit tests are
tasks 2.2/2.3/2.4; test names here are prefixed ``test_board_example_*`` to
avoid colliding with those.
"""

from backend.domain.board import (
    Board,
    Move,
    is_complete_and_valid,
    is_valid_placement,
)

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


def _empty_board() -> Board:
    return [[0 for _ in range(9)] for _ in range(9)]


def test_board_example_valid_placement_on_empty_cell() -> None:
    board = _empty_board()
    assert is_valid_placement(board, 0, 0, 5) is True


def test_board_example_rejects_row_conflict() -> None:
    board = _empty_board()
    board[0][0] = 5
    # Same value elsewhere in row 0 conflicts.
    assert is_valid_placement(board, 0, 8, 5) is False


def test_board_example_rejects_column_conflict() -> None:
    board = _empty_board()
    board[0][0] = 5
    # Same value elsewhere in column 0 conflicts.
    assert is_valid_placement(board, 8, 0, 5) is False


def test_board_example_rejects_box_conflict() -> None:
    board = _empty_board()
    board[0][0] = 5
    # Same value elsewhere in the top-left 3x3 box conflicts.
    assert is_valid_placement(board, 2, 2, 5) is False


def test_board_example_ignores_target_cell_when_it_holds_the_value() -> None:
    # Placing the value already present in the target cell is not a self-conflict.
    assert is_valid_placement(_SOLVED, 0, 0, 5) is True


def test_board_example_complete_and_valid_true_on_solved_board() -> None:
    assert is_complete_and_valid(_SOLVED) is True


def test_board_example_complete_and_valid_false_when_zero_remains() -> None:
    board = [row[:] for row in _SOLVED]
    board[4][4] = 0
    assert is_complete_and_valid(board) is False


def test_board_example_complete_and_valid_false_on_duplicate() -> None:
    board = [row[:] for row in _SOLVED]
    # Break row 0 by duplicating a value (5 already sits at [0][0]).
    board[0][1] = 5
    assert is_complete_and_valid(board) is False


def test_board_example_move_type_is_frozen() -> None:
    move = Move(row=0, col=1, value=7)
    assert (move.row, move.col, move.value) == (0, 1, 7)


# --- Task 2.4: unit tests for board edge cases (Requirements 4.2, 4.5) --------
#
# These example tests exercise the placement rules and solved-detection across
# the edge cases the task names: an empty board, a board one move from
# complete, a contradictory board (duplicate in a row, a column, and a box),
# and cells at 3x3-box boundaries. They append to the existing
# ``test_board_example_*`` cases without modifying them; names are prefixed
# ``test_board_edge_*`` so they stay distinct.

# A second known-good solved board (independent of ``_SOLVED`` above) used as a
# local fixture for these edge cases.
_SOLVED_EDGE: Board = [
    [1, 2, 3, 4, 5, 6, 7, 8, 9],
    [4, 5, 6, 7, 8, 9, 1, 2, 3],
    [7, 8, 9, 1, 2, 3, 4, 5, 6],
    [2, 3, 1, 5, 6, 4, 8, 9, 7],
    [5, 6, 4, 8, 9, 7, 2, 3, 1],
    [8, 9, 7, 2, 3, 1, 5, 6, 4],
    [3, 1, 2, 6, 4, 5, 9, 7, 8],
    [6, 4, 5, 9, 7, 8, 3, 1, 2],
    [9, 7, 8, 3, 1, 2, 6, 4, 5],
]


def _solved_copy() -> Board:
    """A fresh, mutable copy of the local solved fixture."""
    return [row[:] for row in _SOLVED_EDGE]


# --- Empty board -------------------------------------------------------------


def test_board_edge_empty_board_is_not_complete_and_valid() -> None:
    assert is_complete_and_valid(_empty_board()) is False


def test_board_edge_empty_board_accepts_any_digit_at_any_cell() -> None:
    board = _empty_board()
    for row in range(9):
        for col in range(9):
            for value in range(1, 10):
                assert is_valid_placement(board, row, col, value) is True


# --- A board one move from complete ------------------------------------------


def test_board_edge_one_from_complete_is_not_complete_and_valid() -> None:
    board = _solved_copy()
    board[4][4] = 0  # clear a single center cell
    assert is_complete_and_valid(board) is False


def test_board_edge_one_from_complete_accepts_the_correct_missing_value() -> None:
    board = _solved_copy()
    correct = _SOLVED_EDGE[4][4]
    board[4][4] = 0
    assert is_valid_placement(board, 4, 4, correct) is True


def test_board_edge_one_from_complete_rejects_conflicting_values() -> None:
    board = _solved_copy()
    correct = _SOLVED_EDGE[4][4]
    board[4][4] = 0
    # Every other digit already appears in this cell's row, column, or box of a
    # solved board, so no other value is a valid placement here.
    for value in range(1, 10):
        if value == correct:
            continue
        assert is_valid_placement(board, 4, 4, value) is False


def test_board_edge_one_from_complete_becomes_complete_when_refilled() -> None:
    board = _solved_copy()
    correct = _SOLVED_EDGE[4][4]
    board[4][4] = 0
    board[4][4] = correct
    assert is_complete_and_valid(board) is True


# --- Contradictory boards: duplicate in a row, a column, and a box -----------


def test_board_edge_full_board_with_row_duplicate_is_invalid() -> None:
    board = _solved_copy()
    # Row 0 is 1..9; overwrite the 2 with a duplicate 1.
    board[0][1] = 1
    assert is_complete_and_valid(board) is False


def test_board_edge_full_board_with_column_duplicate_is_invalid() -> None:
    board = _solved_copy()
    # Column 0 is 1,4,7,2,5,8,3,6,9; overwrite row 1's 4 with a duplicate 1.
    board[1][0] = 1
    assert is_complete_and_valid(board) is False


def test_board_edge_full_board_with_box_duplicate_is_invalid() -> None:
    board = _solved_copy()
    # Top-left box holds 1,2,3,4,5,6,7,8,9; overwrite [1][1] (a 5) with a
    # duplicate 1 that stays unique within its row and column but collides in
    # the box.
    assert board[1][1] == 5
    board[1][1] = 1
    assert is_complete_and_valid(board) is False


def test_board_edge_placement_rejected_by_existing_row_value() -> None:
    board = _empty_board()
    board[3][0] = 6
    assert is_valid_placement(board, 3, 7, 6) is False


def test_board_edge_placement_rejected_by_existing_column_value() -> None:
    board = _empty_board()
    board[0][3] = 6
    assert is_valid_placement(board, 7, 3, 6) is False


def test_board_edge_placement_rejected_by_existing_box_value() -> None:
    board = _empty_board()
    board[3][3] = 6
    # Same 3x3 box (rows 3-5, cols 3-5), different row and column.
    assert is_valid_placement(board, 5, 5, 6) is False


# --- Boundary cells across different 3x3 boxes -------------------------------


def test_board_edge_box_logic_at_corner_and_center_cells() -> None:
    # A value placed in one box must not be seen as a conflict from a cell in a
    # different box that shares neither row nor column. Seed the four corners
    # and the center, each in a distinct box, with the same value.
    corners_and_center = [(0, 0), (0, 8), (8, 0), (8, 8), (4, 4)]
    for row, col in corners_and_center:
        board = _empty_board()
        board[row][col] = 4
        # A different box, sharing neither row nor column, sees no conflict.
        other_row = 8 - row if row != 4 else 0
        other_col = 8 - col if col != 4 else 0
        assert is_valid_placement(board, other_row, other_col, 4) is True


def test_board_edge_corner_box_detects_own_conflict() -> None:
    # Within the bottom-right box, a duplicate is still rejected at the corner.
    board = _empty_board()
    board[7][7] = 9
    assert is_valid_placement(board, 8, 8, 9) is False
