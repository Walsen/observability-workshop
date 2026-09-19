"""DynamoDB adapter for loading and saving one game per ``(gameId, playerId)``.

Per the design's "DynamoDB repository (adapter)" section, this is a thin adapter
over the boto3 DynamoDB ``Table`` API. It owns no domain logic: it delegates all
(de)serialization to :mod:`backend.data.game` and does nothing but translate a
``load``/``save`` into a single ``get_item``/``put_item`` on the one item keyed
by ``gameId`` (partition key) and ``playerId`` (sort key).

Every access addresses exactly that one ``(gameId, playerId)`` item — there is
no query or scan across players anywhere in this module. That point-access is
the whole of the multi-user isolation model (Requirement 7.3): one player can
neither read nor mutate another player's game because touching an item always
names both key parts.

The table is **injected** (Dependency Inversion): :class:`GamesRepository` takes
the table object, so tests substitute an in-memory fake and the class never
touches AWS on import. The module-level :func:`games_table_from_env` factory
builds the real boto3 table from the ``GAMES_TABLE_NAME`` environment variable
for handlers to use at runtime.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from backend.data.game import Game, game_from_item, game_to_item

# The environment variable CDK sets on each Lambda with the Games table name.
_TABLE_NAME_ENV = "GAMES_TABLE_NAME"


class TableLike(Protocol):
    """The narrow slice of the boto3 DynamoDB ``Table`` the repository needs.

    Only ``get_item`` and ``put_item`` are used, so the repository depends on
    this two-method abstraction rather than the whole boto3 resource. That keeps
    the seam small enough for a hand-rolled fake to satisfy in offline tests and
    keeps mypy happy without boto3 type stubs.
    """

    def get_item(self, **kwargs: Any) -> dict[str, Any]: ...

    def put_item(self, **kwargs: Any) -> dict[str, Any]: ...


class GamesRepository:
    """Loads and saves a single game item, scoped to ``(gameId, playerId)``."""

    def __init__(self, table: TableLike) -> None:
        self._table = table

    def load(self, game_id: str, player_id: str) -> Game | None:
        """Return the stored game for ``(game_id, player_id)``, or ``None``.

        Issues a single ``GetItem`` on the exact composite key. When DynamoDB
        finds no matching item it omits ``Item`` from the response, so an absent
        ``Item`` maps to ``None`` (an unknown game the handler turns into a 404);
        a present item is deserialized by :func:`game_from_item`.
        """
        response = self._table.get_item(
            Key={"gameId": game_id, "playerId": player_id}
        )
        item = response.get("Item")
        if item is None:
            return None
        return game_from_item(item)

    def save(self, game: Game) -> None:
        """Persist ``game`` as the single item keyed by its game and player.

        Issues a ``PutItem`` of the fully-formed item from :func:`game_to_item`,
        whose ``gameId``/``playerId`` attributes are the table's partition and
        sort keys, so the write addresses exactly this one ``(gameId, playerId)``
        pair.
        """
        self._table.put_item(Item=game_to_item(game))


def games_table_from_env() -> Any:
    """Build the real boto3 DynamoDB table from ``GAMES_TABLE_NAME``.

    Used by the Lambda handlers at runtime to obtain the table injected into
    :class:`GamesRepository`. ``boto3`` is imported lazily inside the function so
    importing this module stays AWS-free and offline tests never construct a
    resource. Raises :class:`RuntimeError` if the environment variable is unset,
    failing loudly rather than addressing an unnamed table.
    """
    table_name = os.environ.get(_TABLE_NAME_ENV)
    if not table_name:
        raise RuntimeError(f"{_TABLE_NAME_ENV} environment variable is not set")

    # Lazy import: keeps this module's import AWS-free so offline tests never
    # construct a boto3 resource.
    import boto3

    return boto3.resource("dynamodb").Table(table_name)
