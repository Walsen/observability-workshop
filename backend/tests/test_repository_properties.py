"""Property-based test for the DynamoDB games repository round-trip.

Feature: xray-sudoku-demo, Property 7

This uses hypothesis to assert that any :class:`~backend.data.game.Game` saved
through :class:`~backend.data.games_repository.GamesRepository` and then loaded
by its ``(gameId, playerId)`` key comes back equal, and that the save stored it
as the single item keyed by ``gameId`` (partition key) and ``playerId`` (sort
key). Example-based tests for the same repository live in ``test_repository.py``
(``test_save_*`` / ``test_load_*``); this file keeps the property tests
separate.

The table is injected, so the round-trip runs fully offline against an in-memory
:class:`FakeTable` — no ``moto``, no ``boto3`` resource, and no real AWS. Because
the repository serializes via ``game_to_item`` (boards to JSON strings) and
deserializes via ``game_from_item``, the fake need only store and return the
item dict; the real (de)serialization is what the round-trip exercises.
"""

from __future__ import annotations

from typing import Any

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from backend.data.game import Game, Status
from backend.data.games_repository import GamesRepository
from backend.domain.board import SIZE, Board


class FakeTable:
    """In-memory stand-in for a boto3 DynamoDB ``Table`` resource.

    Implements only the two operations the repository uses — ``get_item`` and
    ``put_item`` — over a dict keyed by the ``(gameId, playerId)`` composite,
    mirroring the real key schema (gameId = PK, playerId = SK). ``get_item``
    returns ``{"Item": item}`` when the pair is present and ``{}`` otherwise,
    exactly as boto3 does (it omits ``Item`` entirely on a miss).
    """

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        composite = (key["gameId"], key["playerId"])
        item = self.items.get(composite)
        return {"Item": item} if item is not None else {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        item = kwargs["Item"]
        composite = (item["gameId"], item["playerId"])
        self.items[composite] = item
        return {}


# --- hypothesis strategies for a Game ----------------------------------------

# gameId / playerId: non-empty strings. Plain text is fine as a dict key; keep
# them simple so shrinking produces readable counterexamples.
_ids = st.text(min_size=1, max_size=32)

# A board is a 9x9 grid of ints in 0..9 (0 = empty, 1..9 filled). The three
# boards on a Game need not be legal Sudoku positions — the repository only
# round-trips them, so arbitrary in-range grids exercise (de)serialization.
_boards = st.lists(
    st.lists(st.integers(min_value=0, max_value=9), min_size=SIZE, max_size=SIZE),
    min_size=SIZE,
    max_size=SIZE,
)

# status is one of the two states game.py admits. Typed as the Status literal
# so the constructed Game type-checks without a cast.
_statuses: st.SearchStrategy[Status] = st.sampled_from(["in_progress", "solved"])

# created_at / updated_at are ISO-8601 strings from the injected clock; any
# non-empty text round-trips, so a small fixed set keeps examples readable.
_timestamps = st.sampled_from(
    [
        "2024-01-01T00:00:00+00:00",
        "2024-06-15T12:30:45+00:00",
        "2025-12-31T23:59:59+00:00",
    ]
)


@st.composite
def _games(draw: st.DrawFn) -> Game:
    """Build an arbitrary :class:`Game` from the strategies above."""
    board: Board = draw(_boards)
    puzzle: Board = draw(_boards)
    solution: Board = draw(_boards)
    return Game(
        game_id=draw(_ids),
        player_id=draw(_ids),
        board=board,
        puzzle=puzzle,
        solution=solution,
        status=draw(_statuses),
        created_at=draw(_timestamps),
        updated_at=draw(_timestamps),
    )


@given(game=_games())
@settings(max_examples=100)
def test_property_game_round_trips_through_the_repository(game: Game) -> None:
    """Property 7: Game state round-trips through the repository.

    Validates: Requirements 2.4, 2.5, 3.2.

    Saving any game and then loading it by its ``(gameId, playerId)`` key
    returns an equal game, and the save leaves exactly one item in the table,
    keyed by ``gameId`` (PK) and ``playerId`` (SK) and carrying those as its
    ``gameId``/``playerId`` attributes.
    """
    table = FakeTable()
    repo = GamesRepository(table)

    repo.save(game)
    loaded = repo.load(game.game_id, game.player_id)

    # The round-trip preserves the whole game.
    assert loaded == game

    # It is stored as exactly one item, keyed by (gameId, playerId).
    assert list(table.items.keys()) == [(game.game_id, game.player_id)]
    stored_item = table.items[(game.game_id, game.player_id)]
    assert stored_item["gameId"] == game.game_id
    assert stored_item["playerId"] == game.player_id


# Feature: xray-sudoku-demo, Property 9


@given(game_a=_games(), game_b=_games())
@settings(max_examples=100)
def test_property_player_isolation_across_item_access(
    game_a: Game, game_b: Game
) -> None:
    """Property 9: Player isolation across item access.

    Validates: Requirements 7.3.

    For any two games whose ``(gameId, playerId)`` composite keys differ,
    saving both to the same repository leaves each addressable only by its own
    key: loading one pair returns that pair's game and never the other's, and a
    write for one pair leaves the other pair's stored item unchanged. The two
    games may share a ``gameId`` and differ only in ``playerId`` (or differ in
    ``gameId``, or both) — the invariant is scoped to the composite key, which
    is the whole of the multi-user isolation model (point access always names
    both key parts, so it never reaches across players).
    """
    key_a = (game_a.game_id, game_a.player_id)
    key_b = (game_b.game_id, game_b.player_id)
    # The property is about two *distinct* pairs. Equal composite keys would be
    # the same item (a plain overwrite, which Property 7 already covers), so
    # discard those examples rather than assert on them.
    assume(key_a != key_b)

    table = FakeTable()
    repo = GamesRepository(table)

    # Save A, then B, into the same table. B's write must not disturb A's item.
    repo.save(game_a)
    repo.save(game_b)

    # Each pair loads back its own game — never the other's.
    assert repo.load(game_a.game_id, game_a.player_id) == game_a
    assert repo.load(game_b.game_id, game_b.player_id) == game_b

    # Saving B left A unchanged: re-loading A after B's write still yields A.
    assert repo.load(game_a.game_id, game_a.player_id) == game_a

    # A cross key — one game's gameId paired with the other's playerId — only
    # exists if it happens to coincide with a key we actually wrote. When the
    # crossed composite matches neither stored pair, the load is a miss (None);
    # when it does match a stored pair, it returns that pair's own game, never
    # the other's. Either way the load never yields a game filed under a
    # different composite key.
    cross_ab = repo.load(game_a.game_id, game_b.player_id)
    if (game_a.game_id, game_b.player_id) == key_a:
        assert cross_ab == game_a
    elif (game_a.game_id, game_b.player_id) == key_b:
        assert cross_ab == game_b
    else:
        assert cross_ab is None

    cross_ba = repo.load(game_b.game_id, game_a.player_id)
    if (game_b.game_id, game_a.player_id) == key_a:
        assert cross_ba == game_a
    elif (game_b.game_id, game_a.player_id) == key_b:
        assert cross_ba == game_b
    else:
        assert cross_ba is None
