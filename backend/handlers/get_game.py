"""The ``get_game`` Lambda handler: ``GET /games/{gameId}`` (task 8.3).

Fetches one player's game: it reads the ``gameId`` off the proxy event's path
parameters and the ``playerId`` off the query string (or, as a fallback, a
header), loads the game scoped to exactly that ``(gameId, playerId)`` pair, and
returns the current board and status — or a ``404`` when no such item exists
(Requirements 3.1, 3.2, 3.3, 7.3).

The module mirrors ``new_game`` structure per Single Responsibility.
:func:`handle` is the testable core: a function of ``(event, deps)`` that owns
the event/domain translation, error mapping, and logging, and takes every
AWS-touching concern — here just the repository — as an injected
:class:`~backend.handlers.bootstrap.Dependencies` bundle, so a test drives it
with an in-memory repository and no cloud. :func:`lambda_handler` is the thin
Lambda entry point that builds the real dependencies and delegates.

Importing this module imports the shared bootstrap, which patches boto3 for
X-Ray once at load, so a cold start is instrumented before any DynamoDB call.
The load itself is scoped to the one ``(gameId, playerId)`` item, which is the
whole of the multi-user isolation model: one player can never read another's
game (Requirement 7.3).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ClientError

from backend.handlers.bootstrap import (
    Dependencies,
    build_runtime_dependencies,
    current_trace_id,
)
from backend.handlers.logging_config import get_logger
from backend.handlers.responses import (
    ProxyResponse,
    bad_request,
    not_found,
    ok,
    server_error,
)

_ENDPOINT = "get_game"
_GAME_ID_FIELD = "gameId"
_PLAYER_ID_FIELD = "playerId"

logger = get_logger(__name__)


class _ValidationError(Exception):
    """Raised when the request fails validation; carries the offending field.

    Kept module-private: it never escapes :func:`handle`, which catches it at the
    boundary and maps it to a 400 naming :attr:`field` (design "Error Handling").
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def handle(event: dict[str, Any], deps: Dependencies) -> ProxyResponse:
    """Fetch a game from ``event`` using injected ``deps``; return a proxy response.

    Validates that the path carries a non-empty ``gameId`` and the request
    carries a non-empty ``playerId`` (query string first, then a header), loads
    the game scoped to that exact pair, and returns ``200`` with the gameId, the
    current board, and the status. The stored solution is deliberately omitted
    from the response so it is never handed to the player.

    Error mapping (design "Error Handling"): a missing/empty ``gameId`` or
    ``playerId`` maps to ``400`` naming the field — bad input never yields a
    ``500`` or a raw stack trace; an unknown ``(gameId, playerId)`` pair maps to
    ``404`` (Requirement 3.3); an unexpected DynamoDB ``ClientError`` maps to a
    generic ``500``. Every response, success or error, carries CORS headers via
    the ``responses`` helpers.
    """
    try:
        game_id, player_id = _parse_keys(event)
    except _ValidationError as exc:
        # Malformed input is a recoverable, expected failure: log at WARNING and
        # return a 400 naming the field. Never a 500, never a stack trace.
        logger.warning(
            "get game rejected: %s",
            exc.message,
            extra={"endpoint": _ENDPOINT, "traceId": current_trace_id()},
        )
        return bad_request(exc.field, exc.message)

    try:
        return _load_game(game_id, player_id, deps)
    except ClientError:
        # The one expected failure past validation is a DynamoDB read error.
        # Log with the stack trace server-side; return a generic 500 to the client.
        logger.exception(
            "get game failed: DynamoDB error",
            extra={
                _GAME_ID_FIELD: game_id,
                _PLAYER_ID_FIELD: player_id,
                "endpoint": _ENDPOINT,
                "traceId": current_trace_id(),
            },
        )
        return server_error()


def _parse_keys(event: dict[str, Any]) -> tuple[str, str]:
    """Extract a non-empty ``(gameId, playerId)`` pair from the proxy event.

    ``gameId`` comes from the path parameters (``event["pathParameters"]``) and
    ``playerId`` from the query string (``event["queryStringParameters"]``),
    falling back to a ``playerId`` header when the query string omits it. Raises
    :class:`_ValidationError` naming the field (which the caller maps to a 400)
    when either is missing, non-string, or whitespace-only. Both are returned
    stripped.
    """
    game_id = _require_field(
        _value_from(event.get("pathParameters"), _GAME_ID_FIELD), _GAME_ID_FIELD
    )
    player_id = _require_field(_player_id_from(event), _PLAYER_ID_FIELD)
    return game_id, player_id


def _player_id_from(event: dict[str, Any]) -> Any:
    """Read ``playerId`` from the query string, falling back to a header.

    The query string is the primary source (design "query/header"); a header is
    accepted only when the query string does not carry a usable value.
    """
    from_query = _value_from(event.get("queryStringParameters"), _PLAYER_ID_FIELD)
    if isinstance(from_query, str) and from_query.strip():
        return from_query
    return _value_from(event.get("headers"), _PLAYER_ID_FIELD)


def _value_from(container: Any, key: str) -> Any:
    """Return ``container[key]`` when ``container`` is a dict, else ``None``.

    API Gateway sets ``pathParameters``/``queryStringParameters``/``headers`` to
    ``None`` (not an empty dict) when there are none, so this tolerates a missing
    or ``None`` container without raising.
    """
    if isinstance(container, dict):
        return container.get(key)
    return None


def _require_field(value: Any, field: str) -> str:
    """Return ``value`` as a non-empty stripped string, or raise for ``field``.

    Raises :class:`_ValidationError` naming ``field`` when the value is missing,
    not a string, or whitespace-only — the caller maps that to a 400.
    """
    if not isinstance(value, str) or not value.strip():
        raise _ValidationError(field, f"{field} is required.")
    return value.strip()


def _load_game(game_id: str, player_id: str, deps: Dependencies) -> ProxyResponse:
    """Load and return the game for ``(game_id, player_id)``, or a 404.

    Addresses exactly the one ``(gameId, playerId)`` item — the whole of the
    multi-user isolation model — so a load for another player's pair simply
    misses and returns ``404`` (Requirements 3.3, 7.3). On a hit, logs at INFO
    and returns a ``200`` carrying the gameId, the current board, and the status;
    the stored solution is intentionally omitted so it is never handed to the
    player. On a miss, logs at INFO and returns :func:`not_found`.
    """
    game = deps.repository.load(game_id, player_id)
    if game is None:
        logger.info(
            "game not found",
            extra={
                _GAME_ID_FIELD: game_id,
                _PLAYER_ID_FIELD: player_id,
                "endpoint": _ENDPOINT,
                "traceId": current_trace_id(),
            },
        )
        return not_found()

    logger.info(
        "game retrieved",
        extra={
            _GAME_ID_FIELD: game_id,
            _PLAYER_ID_FIELD: player_id,
            "endpoint": _ENDPOINT,
            "traceId": current_trace_id(),
        },
    )

    # Return the gameId, the current board, and the status. The solution is
    # intentionally omitted — handing it to the player defeats the game.
    return ok({"gameId": game.game_id, "board": game.board, "status": game.status})


def lambda_handler(event: dict[str, Any], context: Any) -> ProxyResponse:
    """Lambda entry point: build the real dependencies and delegate to :func:`handle`."""
    return handle(event, build_runtime_dependencies())
