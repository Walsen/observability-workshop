"""Example-based tests for the Game model and JSON board serialization (task 6.1).

These pin the behavior of ``backend.data.game``: round-trippable JSON
(de)serialization of boards as nested lists of ints, malformed-input rejection,
the ``Game`` model's field shape, and the pure ``to_item``/``from_item``
mapping to and from the DynamoDB item dict — asserting the DynamoDB attribute
names (``gameId``, ``playerId``, ``createdAt``, ``updatedAt``) and that boards
are stored as JSON strings. No ``boto3`` or AWS is involved; this is the pure
model plus serialization, and the repository (task 6.2) reuses it.
"""

import json
from dataclasses import FrozenInstanceError
from typing import Any

import pytest

from backend.data.game import (
    Game,
    board_from_json,
    board_to_json,
    game_from_item,
    game_to_item,
)
from backend.domain.board import Board

# A small, structurally valid board fixture. Serialization does not require the
# board to satisfy Sudoku rules — only that it is a 9x9 grid of ints — so a
# simple deterministic pattern is enough.
_BOARD: Board = [[(row * 9 + col) % 10 for col in range(9)] for row in range(9)]

_PUZZLE: Board = [[0 for _ in range(9)] for _ in range(9)]

_SOLUTION: Board = [
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


def _game() -> Game:
    return Game(
        game_id="game-123",
        player_id="player-abc",
        board=[row[:] for row in _BOARD],
        puzzle=[row[:] for row in _PUZZLE],
        solution=[row[:] for row in _SOLUTION],
        status="in_progress",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:05:00+00:00",
    )


# --- board_to_json / board_from_json round-trip (normal case) ----------------


def test_board_json_round_trips_board_exactly() -> None:
    restored = board_from_json(board_to_json(_BOARD))
    assert restored == _BOARD


def test_board_to_json_produces_a_json_string_of_nested_ints() -> None:
    text = board_to_json(_BOARD)
    assert isinstance(text, str)
    assert json.loads(text) == _BOARD


def test_board_from_json_returns_a_fresh_board() -> None:
    # The parsed board must not alias any shared structure: mutating it leaves
    # the source fixture untouched.
    restored = board_from_json(board_to_json(_BOARD))
    restored[0][0] = (restored[0][0] + 1) % 10
    assert restored != _BOARD or _BOARD[0][0] != restored[0][0]


# --- board_from_json rejects malformed / wrong-shape input (error case) ------


def test_board_from_json_rejects_non_json() -> None:
    with pytest.raises(ValueError):
        board_from_json("not json at all")


def test_board_from_json_rejects_non_list_json() -> None:
    with pytest.raises(ValueError):
        board_from_json('{"board": []}')


def test_board_from_json_rejects_wrong_row_count() -> None:
    too_few = [[0] * 9 for _ in range(8)]
    with pytest.raises(ValueError):
        board_from_json(json.dumps(too_few))


def test_board_from_json_rejects_wrong_column_count() -> None:
    ragged = [[0] * 9 for _ in range(9)]
    ragged[3] = [0] * 8
    with pytest.raises(ValueError):
        board_from_json(json.dumps(ragged))


def test_board_from_json_rejects_non_int_cell() -> None:
    bad: list[list[Any]] = [[0] * 9 for _ in range(9)]
    bad[0][0] = "x"
    with pytest.raises(ValueError):
        board_from_json(json.dumps(bad))


def test_board_from_json_rejects_bool_cell() -> None:
    # bool is a subclass of int; a JSON boolean is not a valid cell value.
    bad: list[list[Any]] = [[0] * 9 for _ in range(9)]
    bad[0][0] = True
    with pytest.raises(ValueError):
        board_from_json(json.dumps(bad))


# --- Game model shape --------------------------------------------------------


def test_game_constructs_and_holds_its_fields() -> None:
    game = _game()
    assert game.game_id == "game-123"
    assert game.player_id == "player-abc"
    assert game.board == _BOARD
    assert game.puzzle == _PUZZLE
    assert game.solution == _SOLUTION
    assert game.status == "in_progress"
    assert game.created_at == "2024-01-01T00:00:00+00:00"
    assert game.updated_at == "2024-01-01T00:05:00+00:00"


def test_game_is_frozen() -> None:
    game = _game()
    with pytest.raises(FrozenInstanceError):
        game.status = "solved"  # type: ignore[misc]


# --- game_to_item / game_from_item round-trip (DynamoDB item shape) ----------


def test_game_to_item_uses_dynamodb_attribute_names() -> None:
    item = game_to_item(_game())
    assert set(item) == {
        "gameId",
        "playerId",
        "board",
        "puzzle",
        "solution",
        "status",
        "createdAt",
        "updatedAt",
    }


def test_game_to_item_stores_boards_as_json_strings() -> None:
    item = game_to_item(_game())
    assert isinstance(item["board"], str)
    assert isinstance(item["puzzle"], str)
    assert isinstance(item["solution"], str)
    assert json.loads(item["board"]) == _BOARD
    assert json.loads(item["puzzle"]) == _PUZZLE
    assert json.loads(item["solution"]) == _SOLUTION


def test_game_to_item_carries_ids_status_and_timestamps() -> None:
    item = game_to_item(_game())
    assert item["gameId"] == "game-123"
    assert item["playerId"] == "player-abc"
    assert item["status"] == "in_progress"
    assert item["createdAt"] == "2024-01-01T00:00:00+00:00"
    assert item["updatedAt"] == "2024-01-01T00:05:00+00:00"


def test_game_round_trips_through_the_item_dict() -> None:
    game = _game()
    assert game_from_item(game_to_item(game)) == game


def test_game_from_item_rejects_malformed_board_json() -> None:
    item = game_to_item(_game())
    item["board"] = "not json"
    with pytest.raises(ValueError):
        game_from_item(item)
