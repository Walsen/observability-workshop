"""The ``solve`` Lambda handler: ``POST /games/{gameId}/solve`` (task 8.5).

Solves one player's game for them. It reads the ``gameId`` off the proxy event's
path parameters and the ``playerId`` off the body JSON, loads the game scoped to
exactly that ``(gameId, playerId)`` pair, and runs the Solver's backtracking
solve over the *current* board (opening the ``solve-backtracking`` subsegment).
When a completion exists it returns the completed board; when the board admits
no valid completion it returns the documented no-solution response
(Requirements 5.1–5.4, 7.3).

This is the one endpoint that deliberately returns a *full solved board* to the
player — that is its whole purpose (Requirement 5.3). The other endpoints keep
the stored ``solution`` server-side; solve hands back the completion of the
board as it stands.

**solve is read-only.** It loads the game, solves the current board, and
responds; it never persists. That keeps the multi-user isolation model to a
single scoped read, and keeps solve's least-privilege IAM to DynamoDB *read*
only (design "Least-privilege IAM per Lambda", where solve is "read (plus write
only if it persists the solution)"). Nothing here persists, so nothing here
writes — the stored game is unchanged in every case, solvable or not. The
Solver already works on a copy, so the loaded board is never mutated either.

Two outcomes past a successful load are kept distinct, and this is deliberate:

- A **solvable** board returns ``200`` with ``{"solved": true, "board":
  <completed 9x9 board>}`` (Requirement 5.3).
- An **unsolvable** board — one whose current cells carry a direct
  contradiction, so no completion exists — is a valid outcome, not an error. It
  maps to the shared no-solution response: a ``200`` carrying ``{"solved":
  false, "reason": "no_solution"}`` (Requirement 5.4). It is never a ``500``.

The request body shape this handler accepts is::

    {"playerId": "<id>"}

The module mirrors ``new_game``/``get_game``/``submit_move`` structure per Single
Responsibility. :func:`handle` is the testable core — a function of
``(event, deps)`` that owns the event/domain translation, error mapping, and
logging, and takes every AWS-touching concern (the tracer-sharing solver and the
repository) as an injected :class:`~backend.handlers.bootstrap.Dependencies`
bundle, so a test drives it with fakes and no cloud. :func:`lambda_handler` is
the thin Lambda entry point that builds the real dependencies and delegates.

Importing this module imports the shared bootstrap, which patches boto3 for
X-Ray once at load, so a cold start is instrumented before any DynamoDB call.
The load addresses exactly the one ``(gameId, playerId)`` item, which is the
whole of the multi-user isolation model: one player can never solve another's
game (Requirement 7.3).
"""

from __future__ import annotations

import json
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
    no_solution,
    not_found,
    ok,
    server_error,
)

_ENDPOINT = "solve"
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
    """Solve a game from ``event`` using injected ``deps``; return a proxy response.

    Validates that the path carries a non-empty ``gameId`` and the body carries a
    non-empty string ``playerId``, loads the game scoped to that exact pair, and
    runs the Solver over the current board (opening the ``solve-backtracking``
    subsegment). A completion returns ``200`` with the completed board
    (Requirement 5.3); no completion returns the no-solution response
    (Requirement 5.4). The handler never persists — solve is read-only.

    Error mapping (design "Error Handling"): a missing ``gameId`` or a malformed
    body maps to ``400`` naming the field — bad input never yields a ``500`` or a
    raw stack trace; an unknown ``(gameId, playerId)`` pair maps to ``404``
    (Requirement 7.3); an unexpected DynamoDB ``ClientError`` maps to a generic
    ``500``. Every response, success or error, carries CORS headers.
    """
    try:
        game_id, player_id = _parse_request(event)
    except _ValidationError as exc:
        # Malformed input is a recoverable, expected failure: log at WARNING and
        # return a 400 naming the field. Never a 500, never a stack trace.
        logger.warning(
            "solve rejected: %s",
            exc.message,
            extra={"endpoint": _ENDPOINT, "traceId": current_trace_id()},
        )
        return bad_request(exc.field, exc.message)

    try:
        return _solve_game(game_id, player_id, deps)
    except ClientError:
        # The one expected failure past validation is a DynamoDB read error.
        # Log with the stack trace server-side; return a generic 500 to the client.
        logger.exception(
            "solve failed: DynamoDB error",
            extra={
                _GAME_ID_FIELD: game_id,
                _PLAYER_ID_FIELD: player_id,
                "endpoint": _ENDPOINT,
                "traceId": current_trace_id(),
            },
        )
        return server_error()


def _parse_request(event: dict[str, Any]) -> tuple[str, str]:
    """Extract a non-empty ``(gameId, playerId)`` pair from the proxy event, or raise.

    ``gameId`` comes from the path parameters; ``playerId`` comes from the JSON
    body (``{"playerId": "..."}``), consistent with the other POST route. Raises
    :class:`_ValidationError` naming the field (which the caller maps to a 400)
    when the path lacks a usable ``gameId``, the body is missing/not JSON/not an
    object, or ``playerId`` is not a non-empty string. Both are returned stripped.
    """
    game_id = _require_field(
        _value_from(event.get("pathParameters"), _GAME_ID_FIELD), _GAME_ID_FIELD
    )
    parsed_body = _parse_body(event)

    player_id = parsed_body.get(_PLAYER_ID_FIELD)
    if not isinstance(player_id, str) or not player_id.strip():
        raise _ValidationError(
            _PLAYER_ID_FIELD, "playerId must be a non-empty string."
        )
    return game_id, player_id.strip()


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the proxy event's JSON body into a dict, or raise.

    The API Gateway proxy event carries the body as a JSON *string* in
    ``event["body"]``. Raises :class:`_ValidationError` (mapped to a 400) when the
    body is missing, is not valid JSON, or is not a JSON object. The offending
    field is reported as ``playerId`` since a body this malformed carries no
    usable playerId.
    """
    raw_body = event.get("body")
    if raw_body is None:
        raise _ValidationError(_PLAYER_ID_FIELD, "Request body is required.")

    try:
        parsed: Any = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _ValidationError(
            _PLAYER_ID_FIELD, "Request body must be valid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        raise _ValidationError(
            _PLAYER_ID_FIELD, "Request body must be a JSON object."
        )
    return parsed


def _value_from(container: Any, key: str) -> Any:
    """Return ``container[key]`` when ``container`` is a dict, else ``None``.

    API Gateway sets ``pathParameters`` to ``None`` (not an empty dict) when there
    are none, so this tolerates a missing or ``None`` container without raising.
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


def _solve_game(game_id: str, player_id: str, deps: Dependencies) -> ProxyResponse:
    """Load and solve the ``(game_id, player_id)`` game, or return a 404/no-solution.

    Loads the game scoped to that exact pair — a miss returns ``404`` (Requirement
    7.3). Runs the Solver over the current board (opening the
    ``solve-backtracking`` subsegment); the solver works on a copy, so the loaded
    board is never mutated and nothing is persisted (solve is read-only). A
    completion is logged at INFO and returned as a ``200`` carrying the completed
    board (Requirement 5.3). No completion is a valid outcome, not an error: it is
    logged at INFO and returned as the no-solution response (Requirement 5.4).
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

    solved_board = deps.solver.solve(game.board)
    if solved_board is None:
        logger.info(
            "no solution",
            extra={
                _GAME_ID_FIELD: game_id,
                _PLAYER_ID_FIELD: player_id,
                "endpoint": _ENDPOINT,
                "traceId": current_trace_id(),
            },
        )
        return no_solution()

    logger.info(
        "puzzle solved",
        extra={
            _GAME_ID_FIELD: game_id,
            _PLAYER_ID_FIELD: player_id,
            "endpoint": _ENDPOINT,
            "traceId": current_trace_id(),
        },
    )

    # This endpoint's purpose is to hand the completed board back to the player,
    # so — unlike the other handlers — it returns the full solved board.
    return ok({"solved": True, "board": solved_board})


def lambda_handler(event: dict[str, Any], context: Any) -> ProxyResponse:
    """Lambda entry point: build the real dependencies and delegate to :func:`handle`."""
    return handle(event, build_runtime_dependencies())
