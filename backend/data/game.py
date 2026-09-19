"""The ``Game`` domain object and round-trippable board (de)serialization.

Per the design's "Data Model: the Games Table (item attributes)" section, a game
is one item per game per player. This module holds the pure model shared by the
handlers and the DynamoDB repository (task 6.2), plus the serialization helpers
that store the three boards as JSON strings.

It is pure: no ``boto3``, no ``aws_cdk``, no I/O. The repository adapts these
functions to the boto3 table API; here we only turn a :class:`Game` into the
plain item dict shape (and back) and (de)serialize boards. Boards are stored as
JSON strings — a nested list of ints — rather than native DynamoDB structures,
to keep serialization explicit and round-trippable.

Python attribute names on :class:`Game` are snake_case (``game_id``,
``player_id``, ``created_at``, ``updated_at``); the DynamoDB attribute names
(``gameId``, ``playerId``, ``createdAt``, ``updatedAt``) are a serialization
concern and appear only in the item dict produced by :func:`game_to_item`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from backend.domain.board import SIZE, Board

# A game is either being played or finished; these are the only two states the
# design's ``status`` attribute admits.
Status = Literal["in_progress", "solved"]


@dataclass(frozen=True)
class Game:
    """One game's full persisted state for a single ``(gameId, playerId)`` pair.

    Mirrors the Games table item, with snake_case Python names. ``board`` is the
    current 9x9 state (0 = empty), ``puzzle`` the original givens, and
    ``solution`` the unique completion computed at generation. ``created_at`` and
    ``updated_at`` are ISO-8601 strings supplied by the injected clock.
    """

    game_id: str
    player_id: str
    board: Board
    puzzle: Board
    solution: Board
    status: Status
    created_at: str
    updated_at: str


def board_to_json(board: Board) -> str:
    """Serialize a 9x9 board to a compact JSON string of nested ints.

    The inverse of :func:`board_from_json`: ``board_from_json(board_to_json(b))``
    returns a board equal to ``b``.
    """
    return json.dumps(board, separators=(",", ":"))


def board_from_json(text: str) -> Board:
    """Parse a JSON string into a validated 9x9 board of ints.

    Raises :class:`ValueError` when ``text`` is not JSON, is not a 9x9 nested
    list, or holds a non-int cell (booleans are rejected, since ``bool`` is an
    ``int`` subclass but not a valid cell value). Returns a freshly built board
    that shares no structure with any caller state.
    """
    try:
        parsed: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"board is not valid JSON: {exc}") from exc
    return _validate_board(parsed)


def _validate_board(parsed: Any) -> Board:
    """Return ``parsed`` as a board, or raise :class:`ValueError` if malformed."""
    if not isinstance(parsed, list) or len(parsed) != SIZE:
        raise ValueError(f"board must be a list of {SIZE} rows")

    board: Board = []
    for row in parsed:
        if not isinstance(row, list) or len(row) != SIZE:
            raise ValueError(f"each board row must have {SIZE} cells")
        validated_row: list[int] = []
        for cell in row:
            # bool is a subclass of int, so guard it out explicitly before the
            # int check would otherwise accept True/False. ValueError (not
            # TypeError, which TRY004 would prefer) is deliberate: malformed
            # persisted input is a single "bad board" failure mode the handler
            # boundary maps to a 400, so every parse rejection raises ValueError.
            if isinstance(cell, bool) or not isinstance(cell, int):
                raise ValueError("board cells must be ints")  # noqa: TRY004
            validated_row.append(cell)
        board.append(validated_row)
    return board


def game_to_item(game: Game) -> dict[str, str]:
    """Convert a :class:`Game` to the DynamoDB item dict shape.

    Maps snake_case attributes to the table's ``gameId``/``playerId``/
    ``createdAt``/``updatedAt`` names and stores the three boards as JSON
    strings. Pure dict construction — no ``boto3`` — so the repository (task 6.2)
    reuses it to build the item it puts.
    """
    return {
        "gameId": game.game_id,
        "playerId": game.player_id,
        "board": board_to_json(game.board),
        "puzzle": board_to_json(game.puzzle),
        "solution": board_to_json(game.solution),
        "status": game.status,
        "createdAt": game.created_at,
        "updatedAt": game.updated_at,
    }


def game_from_item(item: dict[str, Any]) -> Game:
    """Reconstruct a :class:`Game` from a DynamoDB item dict.

    The inverse of :func:`game_to_item`. Parses each stored board with
    :func:`board_from_json`, so a malformed board string raises
    :class:`ValueError`. The status is read through :func:`_validate_status`,
    which rejects any value outside the two allowed states.
    """
    return Game(
        game_id=str(item["gameId"]),
        player_id=str(item["playerId"]),
        board=board_from_json(item["board"]),
        puzzle=board_from_json(item["puzzle"]),
        solution=board_from_json(item["solution"]),
        status=_validate_status(item["status"]),
        created_at=str(item["createdAt"]),
        updated_at=str(item["updatedAt"]),
    )


def _validate_status(value: Any) -> Status:
    """Return ``value`` as a :data:`Status`, or raise :class:`ValueError`."""
    if value == "in_progress" or value == "solved":
        return value
    raise ValueError(f"status must be 'in_progress' or 'solved', got {value!r}")
