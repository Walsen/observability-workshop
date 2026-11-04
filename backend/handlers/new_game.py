"""The ``new_game`` Lambda handler: ``POST /games`` (task 8.2).

Starts a new game: it reads the player's id off the proxy event, has the Solver
generate a puzzle with a unique solution, assigns a gameId, persists the game
keyed by ``(gameId, playerId)``, and returns the gameId with the initial board
(Requirements 2.1–2.5, 7.3).

The module keeps two jobs apart per Single Responsibility. :func:`handle` is the
pure-ish core: a function of ``(event, deps)`` that does the translation and
owns error mapping and logging, but takes every AWS-touching or nondeterministic
concern — the tracer, solver, repository, clock, id source, and rng — as an
injected :class:`~backend.handlers.bootstrap.Dependencies` bundle, so a test
drives it with fakes and no cloud. :func:`lambda_handler` is the thin Lambda
entry point that builds the real dependencies and delegates.

Importing this module imports the shared bootstrap, which patches boto3 for
X-Ray once at load, so a cold start is instrumented before any DynamoDB call.
"""

from __future__ import annotations

import json
from typing import Any

from botocore.exceptions import ClientError

from backend.data.game import Game
from backend.domain.difficulty import (
    DEFAULT_DIFFICULTY,
    VALID_DIFFICULTY_LEVELS,
    Difficulty,
    is_valid_difficulty,
    resolve,
)
from backend.handlers.bootstrap import (
    Dependencies,
    build_runtime_dependencies,
    current_trace_id,
)
from backend.handlers.logging_config import get_logger
from backend.handlers.responses import ProxyResponse, bad_request, ok, server_error

_ENDPOINT = "new_game"
_PLAYER_ID_FIELD = "playerId"
_DIFFICULTY_FIELD = "difficulty"

logger = get_logger(__name__)


class _ValidationError(Exception):
    """Raised when the request body fails validation; carries the bad field.

    Kept module-private: it never escapes :func:`handle`, which catches it at the
    boundary and maps it to a 400 naming :attr:`field` (design "Error Handling").
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def handle(event: dict[str, Any], deps: Dependencies) -> ProxyResponse:
    """Create a new game from ``event`` using injected ``deps``; return a proxy response.

    Validates that the body carries a non-empty string ``playerId``, generates a
    puzzle (opening the ``generate`` subsegment via the injected solver), assigns
    a gameId and timestamps from the injected id source and clock, saves the game
    scoped to ``(gameId, playerId)``, and returns ``201`` with the gameId, the
    initial board (the givens), and the status. The solution is deliberately
    omitted from the response so it is never handed to the player.

    Error mapping (design "Error Handling"): a malformed body maps to ``400``
    naming ``playerId`` — bad input never yields a ``500`` or a raw stack trace;
    an unexpected DynamoDB ``ClientError`` maps to a generic ``500``. Every
    response, success or error, carries CORS headers via the ``responses`` helpers.
    """
    try:
        player_id, difficulty = _parse_body(event)
    except _ValidationError as exc:
        # Malformed input is a recoverable, expected failure: log at WARNING and
        # return a 400 naming the field. Never a 500, never a stack trace. A
        # difficulty rejection also advertises the accepted set so the player
        # learns the valid values (Requirement 6.3).
        logger.warning(
            "new game rejected: %s",
            exc.message,
            extra={
                "endpoint": _ENDPOINT,
                "field": exc.field,
                "traceId": current_trace_id(),
            },
        )
        valid_values = (
            list(VALID_DIFFICULTY_LEVELS) if exc.field == _DIFFICULTY_FIELD else None
        )
        return bad_request(exc.field, exc.message, valid_values=valid_values)

    try:
        return _create_game(player_id, difficulty, deps)
    except ClientError:
        # The one expected failure past validation is a DynamoDB write error.
        # Log with the stack trace server-side; return a generic 500 to the client.
        logger.exception(
            "new game failed: DynamoDB error",
            extra={
                _PLAYER_ID_FIELD: player_id,
                "endpoint": _ENDPOINT,
                "traceId": current_trace_id(),
            },
        )
        return server_error()


def _parse_body(event: dict[str, Any]) -> tuple[str, Difficulty]:
    """Extract ``(playerId, difficulty)`` from the proxy event body.

    The API Gateway proxy event carries the request body as a JSON *string* in
    ``event["body"]``. This parses it and pulls out a non-empty string
    ``playerId`` and an optional ``difficulty``, raising :class:`_ValidationError`
    (which the caller maps to a 400) when the body is missing, is not valid JSON,
    is not a JSON object, lacks a non-empty string ``playerId``, or carries a
    ``difficulty`` that is present but is not a string or not a registered level.

    Difficulty is read **only** from the JSON body: a difficulty supplied as a
    query-string or path parameter is not consulted here, so it is treated as
    absent and the default applies (Requirement 6.5). An absent ``difficulty``
    defaults to :data:`DEFAULT_DIFFICULTY` with no error (Requirement 7.1); a
    present but invalid one is a 400 naming ``difficulty`` (Requirements 6.1,
    6.2). Whitespace-only ids are rejected; the id is returned stripped.
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

    player_id = parsed.get(_PLAYER_ID_FIELD)
    if not isinstance(player_id, str) or not player_id.strip():
        raise _ValidationError(
            _PLAYER_ID_FIELD, "playerId must be a non-empty string."
        )

    difficulty = _parse_difficulty(parsed)
    return player_id.strip(), difficulty


def _parse_difficulty(body: dict[str, Any]) -> Difficulty:
    """Return the body's ``difficulty`` as a registered level, or the default.

    An absent ``difficulty`` key defaults to :data:`DEFAULT_DIFFICULTY` with no
    error (Requirement 7.1). A present ``difficulty`` must be a string naming a
    registered level; a non-string (Requirement 6.2) or an unknown name
    (Requirement 6.1) raises :class:`_ValidationError` naming the ``difficulty``
    field, which the boundary maps to a 400 listing the valid set — never a 500.
    """
    if _DIFFICULTY_FIELD not in body:
        return DEFAULT_DIFFICULTY

    raw = body[_DIFFICULTY_FIELD]
    if not is_valid_difficulty(raw):
        raise _ValidationError(
            _DIFFICULTY_FIELD,
            "difficulty must be one of: " + ", ".join(VALID_DIFFICULTY_LEVELS) + ".",
        )
    # is_valid_difficulty guarantees ``raw`` is one of the registered names.
    return raw  # type: ignore[return-value]


def _create_game(
    player_id: str, difficulty: Difficulty, deps: Dependencies
) -> ProxyResponse:
    """Generate, persist, and return a new game for ``player_id`` at ``difficulty``.

    Resolves the difficulty strategy from the registry and generates a
    unique-solution puzzle with the injected rng and solver using that level's
    removal count (the ``generate`` subsegment opens here, once regardless of the
    count), assigns the gameId from the injected id source and a single timestamp
    from the injected clock (``created_at`` equals ``updated_at`` at creation),
    stores the game — carrying the chosen ``difficulty`` — scoped to
    ``(gameId, playerId)``, logs the creation at INFO with the difficulty, and
    returns a ``201`` carrying the gameId, the initial board, the status, and the
    difficulty — but not the solution.
    """
    strategy = resolve(difficulty)
    generated = deps.solver.generate(deps.new_rng(), strategy.removal_count)
    game_id = deps.id_source()
    created_at = deps.clock()

    game = Game(
        game_id=game_id,
        player_id=player_id,
        board=generated.puzzle,
        puzzle=generated.puzzle,
        solution=generated.solution,
        status="in_progress",
        difficulty=difficulty,
        created_at=created_at,
        updated_at=created_at,
    )
    deps.repository.save(game)

    logger.info(
        "game created",
        extra={
            "gameId": game_id,
            _PLAYER_ID_FIELD: player_id,
            _DIFFICULTY_FIELD: difficulty,
            "endpoint": _ENDPOINT,
            "traceId": current_trace_id(),
        },
    )

    # Return the gameId, the initial board (the givens), the status, and the
    # chosen difficulty. The solution is intentionally omitted — handing it to
    # the player defeats the game.
    return ok(
        {
            "gameId": game_id,
            "board": game.board,
            "status": game.status,
            "difficulty": game.difficulty,
        },
        status=201,
    )


def lambda_handler(event: dict[str, Any], context: Any) -> ProxyResponse:
    """Lambda entry point: build the real dependencies and delegate to :func:`handle`."""
    return handle(event, build_runtime_dependencies())
