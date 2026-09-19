"""Example-based tests for the DynamoDB games repository (task 6.2).

These pin the behavior of ``backend.data.games_repository.GamesRepository``: a
thin adapter over a boto3 DynamoDB ``Table`` that loads and saves exactly the
one item identified by ``(gameId, playerId)``. The table is injected, so these
tests substitute a hand-rolled in-memory fake implementing only ``get_item`` and
``put_item`` — no ``moto``, no ``boto3`` resource, and no real AWS. The
serialization to and from the item dict is delegated to ``backend.data.game``
(task 6.1); this suite asserts the round-trip through the repository, the
missing-item case, and the exact ``Key``/``Item`` shapes handed to the table.

Property tests for the round-trip (Property 7) and player isolation (Property 9)
are separate tasks (6.3/6.4) and are intentionally not implemented here.
"""

from typing import Any

from backend.data.game import Game, game_to_item
from backend.data.games_repository import GamesRepository
from backend.domain.board import Board

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


def _game(game_id: str = "game-123", player_id: str = "player-abc") -> Game:
    return Game(
        game_id=game_id,
        player_id=player_id,
        board=[row[:] for row in _BOARD],
        puzzle=[row[:] for row in _PUZZLE],
        solution=[row[:] for row in _SOLUTION],
        status="in_progress",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:05:00+00:00",
    )


class FakeTable:
    """In-memory stand-in for a boto3 DynamoDB ``Table`` resource.

    Implements only the two operations the repository uses — ``get_item`` and
    ``put_item`` — over a dict keyed by the ``(gameId, playerId)`` composite,
    mirroring the real key schema (gameId = PK, playerId = SK). It also records
    the calls it received so tests can assert the exact ``Key``/``Item`` shapes
    the repository builds.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.get_item_calls: list[dict[str, Any]] = []
        self.put_item_calls: list[dict[str, Any]] = []

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self.get_item_calls.append(kwargs)
        key = kwargs["Key"]
        composite = (key["gameId"], key["playerId"])
        item = self.items.get(composite)
        # boto3 omits "Item" from the response entirely when nothing matches.
        return {"Item": item} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self.put_item_calls.append(kwargs)
        item = kwargs["Item"]
        composite = (item["gameId"], item["playerId"])
        self.items[composite] = item
        return {}


# --- save then load round-trips (normal case) --------------------------------


def test_save_then_load_round_trips_the_game() -> None:
    table = FakeTable()
    repo = GamesRepository(table)
    game = _game()

    repo.save(game)
    loaded = repo.load(game.game_id, game.player_id)

    assert loaded == game


# --- load returns None for a missing item (edge case) ------------------------


def test_load_returns_none_for_missing_item() -> None:
    table = FakeTable()
    repo = GamesRepository(table)

    assert repo.load("no-such-game", "no-such-player") is None


def test_load_returns_none_when_only_a_different_pair_is_stored() -> None:
    table = FakeTable()
    repo = GamesRepository(table)
    repo.save(_game(game_id="game-1", player_id="player-1"))

    # Same gameId, different playerId is a different item — must not be found.
    assert repo.load("game-1", "player-2") is None


# --- correct Key / Item shapes -----------------------------------------------


def test_load_addresses_exactly_the_game_and_player_key() -> None:
    table = FakeTable()
    repo = GamesRepository(table)

    repo.load("game-xyz", "player-42")

    assert len(table.get_item_calls) == 1
    assert table.get_item_calls[0]["Key"] == {
        "gameId": "game-xyz",
        "playerId": "player-42",
    }


def test_save_puts_the_full_item_keyed_by_game_and_player() -> None:
    table = FakeTable()
    repo = GamesRepository(table)
    game = _game()

    repo.save(game)

    assert len(table.put_item_calls) == 1
    item = table.put_item_calls[0]["Item"]
    # The keys carried on the item are the PK/SK the table is keyed by.
    assert item["gameId"] == game.game_id
    assert item["playerId"] == game.player_id
    # The stored item is exactly the serialized game (delegated to game.py).
    assert item == game_to_item(game)


# --- each method issues exactly one operation, the right one (task 6.5) ------


def test_load_issues_a_single_get_item_and_no_put_item() -> None:
    table = FakeTable()
    repo = GamesRepository(table)

    repo.load("game-xyz", "player-42")

    # load reads and only reads: one GetItem, zero PutItem.
    assert len(table.get_item_calls) == 1
    assert table.put_item_calls == []


def test_save_issues_a_single_put_item_and_no_get_item() -> None:
    table = FakeTable()
    repo = GamesRepository(table)

    repo.save(_game())

    # save writes and only writes: one PutItem, zero GetItem.
    assert len(table.put_item_calls) == 1
    assert table.get_item_calls == []


# --- re-saving the same pair updates the one item in place (task 6.5) --------


def test_second_save_for_same_pair_overwrites_the_item_in_place() -> None:
    table = FakeTable()
    repo = GamesRepository(table)
    first = _game()
    # Same (gameId, playerId) but a later state: a filled-in board and solved.
    updated = Game(
        game_id=first.game_id,
        player_id=first.player_id,
        board=[row[:] for row in _SOLUTION],
        puzzle=first.puzzle,
        solution=first.solution,
        status="solved",
        created_at=first.created_at,
        updated_at="2024-01-01T00:10:00+00:00",
    )

    repo.save(first)
    repo.save(updated)

    # Two writes issued, but they collapse onto the one keyed item — no
    # duplicate row — and a load returns the latest state.
    assert len(table.put_item_calls) == 2
    assert len(table.items) == 1
    assert repo.load(first.game_id, first.player_id) == updated


# --- boards cross the repository boundary as JSON strings (task 6.5) ---------


def test_saved_item_stores_boards_as_json_strings() -> None:
    table = FakeTable()
    repo = GamesRepository(table)
    game = _game()

    repo.save(game)

    item = table.put_item_calls[0]["Item"]
    # The three boards persist as JSON strings, not native nested lists, so the
    # item that reaches the table matches the serialized shape game.py defines.
    for attribute in ("board", "puzzle", "solution"):
        assert isinstance(item[attribute], str)
