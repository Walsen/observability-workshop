"""The ``submit_move`` Lambda handler: ``POST /games/{gameId}/moves`` (task 8.4).

Applies one player's move to their game. It reads the ``gameId`` off the proxy
event's path parameters and the ``playerId`` and ``move`` off the body JSON,
loads the game scoped to exactly that ``(gameId, playerId)`` pair, validates the
move against the *current* board via the Solver (opening the ``validate``
subsegment), and — when the move is a legal placement — applies it, bumps the
timestamp, and persists the updated game. If that move completes the board, the
Solver's ``check-solution`` confirms it and the game's status becomes ``solved``
(Requirements 4.1–4.5, 7.3).

Two "invalid" outcomes are kept distinct, and this is deliberate:

- A **malformed** move — a body that is not JSON, a missing ``move``, or a
  ``row``/``col``/``value`` of the wrong type or out of range — is a client
  error. It maps to ``400`` naming the offending field and never reaches the
  domain. Bad input never yields a ``500`` or a raw stack trace.
- A **well-formed but illegal** move — a placement that conflicts within its
  row, column, or box, or lands on an already-filled cell — is a normal
  gameplay outcome, not a client error. It maps to ``200`` with
  ``{"accepted": false, "reason": "invalid_move"}`` and leaves the stored board
  unchanged. This is the "response indicating the Move is invalid" of
  Requirement 4.3.

The request body shape this handler accepts is::

    {"playerId": "<id>", "move": {"row": <0-8>, "col": <0-8>, "value": <1-9>}}

The module mirrors ``new_game``/``get_game`` structure per Single Responsibility.
:func:`handle` is the testable core — a function of ``(event, deps)`` that owns
the event/domain translation, error mapping, and logging, and takes every
AWS-touching or nondeterministic concern (the tracer, solver, repository, and
clock) as an injected :class:`~backend.handlers.bootstrap.Dependencies` bundle,
so a test drives it with fakes and no cloud. :func:`lambda_handler` is the thin
Lambda entry point that builds the real dependencies and delegates.

Importing this module imports the shared bootstrap, which patches boto3 for
X-Ray once at load, so a cold start is instrumented before any DynamoDB call.
The load and save address exactly the one ``(gameId, playerId)`` item, which is
the whole of the multi-user isolation model: one player can never read or mutate
another's game (Requirement 7.3).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from botocore.exceptions import ClientError

from backend.domain.board import SIZE, Board, Move
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

_ENDPOINT = "submit_move"
_GAME_ID_FIELD = "gameId"
_PLAYER_ID_FIELD = "playerId"
_MOVE_FIELD = "move"
_MIN_VALUE = 1

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
    """Apply a move from ``event`` using injected ``deps``; return a proxy response.

    Validates that the path carries a non-empty ``gameId`` and the body carries a
    non-empty string ``playerId`` and a well-formed ``move`` (``row``/``col`` ints
    in ``0..8``, ``value`` an int in ``1..9``), loads the game scoped to that
    exact pair, and validates the move against the current board (opening the
    ``validate`` subsegment). A legal move is applied to a copy of the board, the
    game's timestamp is bumped from the injected clock, and the updated game is
    saved; if the move completes the board correctly (confirmed via the
    ``check-solution`` subsegment) the status becomes ``solved``. The response
    carries the updated board and the ``accepted``/``solved`` flags — never the
    stored solution.

    Error mapping (design "Error Handling"): a missing ``gameId`` or a malformed
    body/move maps to ``400`` naming the field — bad input never yields a ``500``
    or a raw stack trace; an unknown ``(gameId, playerId)`` pair maps to ``404``
    (Requirement 7.3); a legal-but-illegal placement maps to a ``200`` rejection
    (Requirement 4.3); an unexpected DynamoDB ``ClientError`` maps to a generic
    ``500``. Every response, success or error, carries CORS headers.
    """
    try:
        game_id, player_id, move = _parse_request(event)
    except _ValidationError as exc:
        # Malformed input is a recoverable, expected failure: log at WARNING and
        # return a 400 naming the field. Never a 500, never a stack trace.
        logger.warning(
            "submit move rejected: %s",
            exc.message,
            extra={"endpoint": _ENDPOINT, "traceId": current_trace_id()},
        )
        return bad_request(exc.field, exc.message)

    try:
        return _apply_move(game_id, player_id, move, deps)
    except ClientError:
        # The one expected failure past validation is a DynamoDB read/write error.
        # Log with the stack trace server-side; return a generic 500 to the client.
        logger.exception(
            "submit move failed: DynamoDB error",
            extra={
                _GAME_ID_FIELD: game_id,
                _PLAYER_ID_FIELD: player_id,
                "endpoint": _ENDPOINT,
                "traceId": current_trace_id(),
            },
        )
        return server_error()


def _parse_request(event: dict[str, Any]) -> tuple[str, str, Move]:
    """Extract ``(gameId, playerId, Move)`` from the proxy event, or raise.

    ``gameId`` comes from the path parameters; ``playerId`` and ``move`` come from
    the JSON body. Raises :class:`_ValidationError` naming the field (which the
    caller maps to a 400) when the path lacks a usable ``gameId``, the body is
    missing/not JSON/not an object, ``playerId`` is not a non-empty string, or the
    ``move`` is missing or has a ``row``/``col``/``value`` of the wrong type or out
    of range.
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

    move = _parse_move(parsed_body.get(_MOVE_FIELD))
    return game_id, player_id.strip(), move


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the proxy event's JSON body into a dict, or raise.

    The API Gateway proxy event carries the body as a JSON *string* in
    ``event["body"]``. Raises :class:`_ValidationError` (mapped to a 400) when the
    body is missing, is not valid JSON, or is not a JSON object. The offending
    field is reported as ``move`` since a body this malformed carries no usable
    move.
    """
    raw_body = event.get("body")
    if raw_body is None:
        raise _ValidationError(_MOVE_FIELD, "Request body is required.")

    try:
        parsed: Any = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError) as exc:
        raise _ValidationError(
            _MOVE_FIELD, "Request body must be valid JSON."
        ) from exc

    if not isinstance(parsed, dict):
        raise _ValidationError(_MOVE_FIELD, "Request body must be a JSON object.")
    return parsed


def _parse_move(raw_move: Any) -> Move:
    """Build a :class:`Move` from the body's ``move`` object, or raise.

    Requires ``raw_move`` to be a JSON object with ``row`` and ``col`` integers in
    ``0..8`` and ``value`` an integer in ``1..9``. Raises :class:`_ValidationError`
    naming the specific offending subfield (``move``, ``move.row``, ``move.col``,
    or ``move.value``), which the caller maps to a 400 — a malformed move is
    always a client error, never a 500. Booleans are rejected even though ``bool``
    subclasses ``int``.
    """
    if not isinstance(raw_move, dict):
        raise _ValidationError(_MOVE_FIELD, "move must be an object.")

    row = _require_index(raw_move.get("row"), "move.row")
    col = _require_index(raw_move.get("col"), "move.col")
    value = _require_value(raw_move.get("value"), "move.value")
    return Move(row=row, col=col, value=value)


def _require_index(raw: Any, field: str) -> int:
    """Return ``raw`` as an int cell index in ``0..8``, or raise for ``field``."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _ValidationError(field, f"{field} must be an integer.")
    if not (0 <= raw < SIZE):
        raise _ValidationError(field, f"{field} must be in 0..{SIZE - 1}.")
    return raw


def _require_value(raw: Any, field: str) -> int:
    """Return ``raw`` as an int placement value in ``1..9``, or raise for ``field``."""
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _ValidationError(field, f"{field} must be an integer.")
    if not (_MIN_VALUE <= raw <= SIZE):
        raise _ValidationError(field, f"{field} must be in {_MIN_VALUE}..{SIZE}.")
    return raw


def _value_from(container: Any, key: str) -> Any:
    """Return ``container[key]`` when ``container`` is a dict, else ``None``.

    API Gateway sets ``pathParameters`` to ``None`` (not an empty dict) when there
    are none, so this tolerates a missing or ``None`` container without raising.
    """
    if isinstance(container, dict):
        return container.get(key)
    return None


def _require_field(value: Any, field: str) -> str:
    """Return ``value`` as a non-empty stripped string, or raise for ``field``."""
    if not isinstance(value, str) or not value.strip():
        raise _ValidationError(field, f"{field} is required.")
    return value.strip()


def _apply_move(
    game_id: str, player_id: str, move: Move, deps: Dependencies
) -> ProxyResponse:
    """Validate and apply ``move`` to the ``(game_id, player_id)`` game.

    Loads the game scoped to that exact pair — a miss returns ``404`` (Requirement
    7.3). Validates the move against the current board via the Solver (opening the
    ``validate`` subsegment); a well-formed but illegal placement is a rejected
    move, logged at WARNING and returned as a ``200`` ``accepted: false`` without
    touching the store (Requirement 4.3). A legal move is applied to a copy of the
    board, the game's ``updated_at`` is bumped from the injected clock, and the
    updated game is saved (Requirement 4.4); if the move completes the board
    (confirmed via ``check-solution``) the status becomes ``solved`` and that is
    persisted (Requirement 4.5). The response carries the updated board and flags,
    never the solution.
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

    if not deps.solver.validate_move(game.board, move):
        logger.warning(
            "move rejected",
            extra={
                _GAME_ID_FIELD: game_id,
                _PLAYER_ID_FIELD: player_id,
                "endpoint": _ENDPOINT,
                "traceId": current_trace_id(),
            },
        )
        return ok({"accepted": False, "reason": "invalid_move"})

    updated_board = _board_with_move(game.board, move)
    solved = deps.solver.check_solution(updated_board)
    status = "solved" if solved else game.status

    updated_game = replace(
        game,
        board=updated_board,
        status=status,
        updated_at=deps.clock(),
    )
    deps.repository.save(updated_game)

    logger.info(
        "move accepted",
        extra={
            _GAME_ID_FIELD: game_id,
            _PLAYER_ID_FIELD: player_id,
            "endpoint": _ENDPOINT,
            "traceId": current_trace_id(),
        },
    )

    # Return the updated board and the accepted/solved flags plus the status. The
    # stored solution is intentionally omitted so it is never handed to the player.
    return ok(
        {
            "accepted": True,
            "solved": solved,
            "status": updated_game.status,
            "board": updated_board,
        }
    )


def _board_with_move(board: Board, move: Move) -> Board:
    """Return a deep copy of ``board`` with ``move`` applied to its target cell.

    Copies every row so the returned board shares no structure with the input —
    the caller's (frozen :class:`Game`'s) board is never mutated in place, per the
    determinism and immutability rules.
    """
    updated = [row[:] for row in board]
    updated[move.row][move.col] = move.value
    return updated


def lambda_handler(event: dict[str, Any], context: Any) -> ProxyResponse:
    """Lambda entry point: build the real dependencies and delegate to :func:`handle`."""
    return handle(event, build_runtime_dependencies())
