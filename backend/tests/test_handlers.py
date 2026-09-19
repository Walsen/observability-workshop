"""Handler tests for the Lambda proxy handlers (tasks 8.2–8.5, 8.7).

These run fully offline: no ``boto3``, no AWS credentials, no network. Each test
constructs a :class:`~backend.handlers.bootstrap.Dependencies` bundle directly
from test doubles — a :class:`~backend.domain.tracing.FakeTracer`-backed
:class:`~backend.domain.solver.Solver` (or a canned fake solver for speed), an
in-memory :class:`InMemoryGames` repository, and pinned clock, id source, and
seeded rng — so a handler's core ``handle(event, deps)`` function is exercised
with the real domain wiring but no cloud dependency. ``build_runtime_dependencies``
(which needs AWS) is never called here.

``InMemoryGames`` lives here so the 8.3–8.5 handler tests can reuse it: it is a
drop-in for :class:`~backend.data.games_repository.GamesRepository`, storing the
one item per ``(gameId, playerId)`` pair over a plain dict.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.data.game import Game
from backend.domain.board import SIZE, Board, is_complete_and_valid
from backend.domain.solver import GeneratedPuzzle, Solver
from backend.domain.tracing import FakeTracer
from backend.handlers import get_game, new_game, solve, submit_move
from backend.handlers.bootstrap import Dependencies

# --- a puzzle/solution the fake solver hands back, deterministically ---------

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

# The givens: the solution with a handful of cells blanked. It is not itself a
# uniqueness-checked puzzle — this is a canned value for handler tests, where we
# care about the handler's translation, not the generator's carving.
_PUZZLE: Board = [row[:] for row in _SOLUTION]
for _r, _c in ((0, 0), (1, 4), (4, 8), (8, 2)):
    _PUZZLE[_r][_c] = 0


class InMemoryGames:
    """In-memory stand-in for ``GamesRepository`` reusable across handler tests.

    Stores one game per ``(game_id, player_id)`` pair over a dict, mirroring the
    repository's point-access contract: ``save`` overwrites exactly that pair's
    item and ``load`` returns it or ``None``. It records how often each method
    ran so a test can assert the repository was (or was not) written.
    """

    def __init__(self) -> None:
        self.games: dict[tuple[str, str], Any] = {}
        self.save_calls = 0
        self.load_calls = 0

    def save(self, game: Any) -> None:
        self.save_calls += 1
        self.games[(game.game_id, game.player_id)] = game

    def load(self, game_id: str, player_id: str) -> Any | None:
        self.load_calls += 1
        return self.games.get((game_id, player_id))


class CannedSolver:
    """A solver whose ``generate`` returns a fixed puzzle but still traces.

    Opens the real ``generate`` subsegment on the injected tracer (so the
    subsegment assertion holds) and returns the canned ``_PUZZLE``/``_SOLUTION``
    without running the (slower) real generator, keeping handler tests fast and
    fully deterministic.
    """

    def __init__(self, tracer: FakeTracer) -> None:
        self._tracer = tracer

    def generate(self, rng: Any) -> GeneratedPuzzle:
        with self._tracer.subsegment("generate"):
            return GeneratedPuzzle(
                puzzle=[row[:] for row in _PUZZLE],
                solution=[row[:] for row in _SOLUTION],
            )


def _deps(
    *,
    tracer: FakeTracer,
    repository: InMemoryGames,
    solver: Any,
    game_id: str = "game-1",
    now: str = "2024-01-01T00:00:00+00:00",
    seed: int = 1234,
) -> Dependencies:
    """Assemble a Dependencies bundle from injected test doubles."""
    import random

    return Dependencies(
        tracer=tracer,  # type: ignore[arg-type]
        solver=solver,
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: now,
        id_source=lambda: game_id,
        new_rng=lambda *, seed=seed: random.Random(seed),
    )


def _post_games(body: Any, *, raw: str | None = None) -> dict[str, Any]:
    """Build a POST /games proxy event; ``raw`` overrides the JSON body string."""
    return {"body": raw if raw is not None else json.dumps(body)}


# --- happy path --------------------------------------------------------------


def test_new_game_returns_201_with_game_id_and_initial_board() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games({"playerId": "player-abc"}), deps)

    assert response["statusCode"] == 201
    payload = json.loads(response["body"])
    assert payload["gameId"] == "game-1"
    assert payload["status"] == "in_progress"
    board = payload["board"]
    assert len(board) == SIZE and all(len(row) == SIZE for row in board)
    # The returned initial board is the puzzle givens.
    assert board == _PUZZLE


def test_new_game_saves_the_game_keyed_by_game_and_player() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    new_game.handle(_post_games({"playerId": "player-abc"}), deps)

    assert repo.save_calls == 1
    stored = repo.games[("game-1", "player-abc")]
    # The stored board and puzzle are the givens; the solution is kept server-side.
    assert stored.board == _PUZZLE
    assert stored.puzzle == _PUZZLE
    assert stored.solution == _SOLUTION
    assert stored.status == "in_progress"
    assert stored.created_at == "2024-01-01T00:00:00+00:00"
    assert stored.updated_at == stored.created_at


def test_new_game_response_does_not_leak_the_solution() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games({"playerId": "player-abc"}), deps)

    payload = json.loads(response["body"])
    assert "solution" not in payload


def test_new_game_opens_the_generate_subsegment() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    new_game.handle(_post_games({"playerId": "player-abc"}), deps)

    assert "generate" in tracer.entered


def test_new_game_with_the_real_solver_generates_a_unique_solution_board() -> None:
    """End-to-end with the real Solver: the stored solution actually solves."""
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=Solver(tracer), seed=7)

    response = new_game.handle(_post_games({"playerId": "p"}), deps)

    assert response["statusCode"] == 201
    stored = repo.games[("game-1", "p")]
    assert is_complete_and_valid(stored.solution)
    assert "generate" in tracer.entered


# --- CORS on success ---------------------------------------------------------


def test_new_game_success_carries_cors_headers() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games({"playerId": "player-abc"}), deps)

    assert "Access-Control-Allow-Origin" in response["headers"]


# --- bad input: never 500, always names the field, never writes --------------


def test_new_game_missing_player_id_is_400_naming_player_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games({}), deps)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "playerId"
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.save_calls == 0


def test_new_game_non_string_player_id_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games({"playerId": 123}), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.save_calls == 0


def test_new_game_empty_player_id_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games({"playerId": "   "}), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.save_calls == 0


def test_new_game_body_not_json_is_400_not_500() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games(None, raw="{not json"), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.save_calls == 0


def test_new_game_missing_body_is_400_not_500() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle({}, deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.save_calls == 0


def test_new_game_body_not_an_object_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))

    response = new_game.handle(_post_games(None, raw='"just a string"'), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.save_calls == 0


# --- get_game (task 8.3) -----------------------------------------------------


def _stored_game(
    *,
    game_id: str = "game-1",
    player_id: str = "player-abc",
    status: str = "in_progress",
) -> Game:
    """Build a Game to seed InMemoryGames with, for get_game load tests."""
    return Game(
        game_id=game_id,
        player_id=player_id,
        board=[row[:] for row in _PUZZLE],
        puzzle=[row[:] for row in _PUZZLE],
        solution=[row[:] for row in _SOLUTION],
        status=status,  # type: ignore[arg-type]
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )


def _get_game_event(
    *,
    game_id: str | None = "game-1",
    player_id: str | None = "player-abc",
    player_id_in_header: bool = False,
) -> dict[str, Any]:
    """Build a GET /games/{gameId} proxy event.

    ``gameId`` rides the path parameters; ``playerId`` rides the query string by
    default, or a header when ``player_id_in_header`` is set. Passing ``None``
    for either omits it, to exercise the missing-field validation.
    """
    path_params: dict[str, str] = {}
    if game_id is not None:
        path_params["gameId"] = game_id

    query_params: dict[str, str] = {}
    headers: dict[str, str] = {}
    if player_id is not None:
        if player_id_in_header:
            headers["playerId"] = player_id
        else:
            query_params["playerId"] = player_id

    return {
        "pathParameters": path_params or None,
        "queryStringParameters": query_params or None,
        "headers": headers or None,
    }


def _get_game_deps(tracer: FakeTracer, repo: InMemoryGames) -> Dependencies:
    """get_game deps: it never uses the solver, but the bundle requires one."""
    return _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer))


def test_get_game_returns_200_with_board_status_and_game_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    repo.save(_stored_game(game_id="game-1", player_id="player-abc"))
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(
        _get_game_event(game_id="game-1", player_id="player-abc"), deps
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["gameId"] == "game-1"
    assert payload["status"] == "in_progress"
    assert payload["board"] == _PUZZLE


def test_get_game_loads_the_pair_it_was_asked_for() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    repo.save(_stored_game(game_id="game-1", player_id="player-abc"))
    deps = _get_game_deps(tracer, repo)

    get_game.handle(_get_game_event(game_id="game-1", player_id="player-abc"), deps)

    assert repo.load_calls == 1


def test_get_game_response_does_not_leak_the_solution() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    repo.save(_stored_game())
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(_get_game_event(), deps)

    payload = json.loads(response["body"])
    assert "solution" not in payload


def test_get_game_accepts_player_id_from_a_header() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    repo.save(_stored_game(game_id="game-1", player_id="player-abc"))
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(
        _get_game_event(
            game_id="game-1", player_id="player-abc", player_id_in_header=True
        ),
        deps,
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["gameId"] == "game-1"


def test_get_game_success_carries_cors_headers() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    repo.save(_stored_game())
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(_get_game_event(), deps)

    assert "Access-Control-Allow-Origin" in response["headers"]


def test_get_game_unknown_item_is_404_with_cors() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()  # empty: nothing stored
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(
        _get_game_event(game_id="does-not-exist", player_id="player-abc"), deps
    )

    assert response["statusCode"] == 404
    assert "Access-Control-Allow-Origin" in response["headers"]


def test_get_game_missing_game_id_is_400_naming_game_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(_get_game_event(game_id=None), deps)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "gameId"
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.load_calls == 0


def test_get_game_missing_player_id_is_400_naming_player_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(_get_game_event(player_id=None), deps)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "playerId"
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.load_calls == 0


def test_get_game_empty_player_id_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(_get_game_event(player_id="   "), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.load_calls == 0


def test_get_game_enforces_player_isolation_at_the_handler() -> None:
    """A game stored for playerA is 404 when requested with playerB."""
    tracer = FakeTracer()
    repo = InMemoryGames()
    repo.save(_stored_game(game_id="game-1", player_id="player-A"))
    deps = _get_game_deps(tracer, repo)

    response = get_game.handle(
        _get_game_event(game_id="game-1", player_id="player-B"), deps
    )

    assert response["statusCode"] == 404


# --- submit_move (task 8.4) --------------------------------------------------
#
# Body shape (documented): the request body is a JSON object carrying the
# playerId and a nested move, e.g.
#     {"playerId": "player-abc", "move": {"row": 0, "col": 0, "value": 1}}
# gameId rides the path parameters.
#
# Status decision (documented): a *malformed* move — missing, wrong type, or out
# of range — is a client error and maps to 400 naming the offending field. A
# *well-formed but illegal* move (a conflicting placement or a move onto a filled
# cell) is a normal gameplay outcome, not a client error, so it maps to 200 with
# {"accepted": false, "reason": "invalid_move"} and the board is left unchanged.
# This honors Requirement 4.3 ("return a response indicating the Move is
# invalid") while keeping bad input strictly 400, never 500.


def _submit_move_event(
    *,
    game_id: str | None = "game-1",
    player_id: str | None = "player-abc",
    move: Any = None,
    raw: str | None = None,
) -> dict[str, Any]:
    """Build a POST /games/{gameId}/moves proxy event.

    ``gameId`` rides the path parameters; ``playerId`` and ``move`` ride the JSON
    body. Passing ``None`` for ``game_id`` omits the path parameter; ``raw``
    overrides the JSON body string wholesale (to exercise malformed-body paths).
    """
    path_params: dict[str, str] = {}
    if game_id is not None:
        path_params["gameId"] = game_id

    if raw is not None:
        body = raw
    else:
        payload: dict[str, Any] = {}
        if player_id is not None:
            payload["playerId"] = player_id
        if move is not None:
            payload["move"] = move
        body = json.dumps(payload)

    return {"pathParameters": path_params or None, "body": body}


def _submit_move_deps(tracer: FakeTracer, repo: InMemoryGames) -> Dependencies:
    """submit_move deps: a real tracer-backed Solver (fast) and in-memory repo."""
    return _deps(tracer=tracer, repository=repo, solver=Solver(tracer))


def _seed(repo: InMemoryGames, game: Game) -> None:
    """Store ``game`` in ``repo`` and reset its call counters to zero.

    Seeding a game is test setup, not the behavior under test, so resetting
    ``save_calls``/``load_calls`` afterward lets a test assert precisely how many
    times the handler touched the repository.
    """
    repo.save(game)
    repo.save_calls = 0
    repo.load_calls = 0


def _empty_cell_of(puzzle: Board) -> tuple[int, int]:
    """Return the first empty (0) cell of ``puzzle`` in row-major order."""
    for r in range(SIZE):
        for c in range(SIZE):
            if puzzle[r][c] == 0:
                return r, c
    raise AssertionError("puzzle has no empty cell")


def _one_from_complete_game(
    *, game_id: str = "game-1", player_id: str = "player-abc"
) -> tuple[Game, int, int, int]:
    """A game whose board equals the solution except one empty cell.

    Returns the game plus ``(row, col, value)``: the single empty cell and the
    correct value that completes the board. Submitting that move should solve it.
    """
    board = [row[:] for row in _SOLUTION]
    row, col = 4, 4
    correct = _SOLUTION[row][col]
    board[row][col] = 0
    game = Game(
        game_id=game_id,
        player_id=player_id,
        board=board,
        puzzle=board,
        solution=[r[:] for r in _SOLUTION],
        status="in_progress",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )
    return game, row, col, correct


# --- happy path: a valid move on an incomplete board -------------------------


def test_submit_move_valid_move_returns_200_accepted_and_updates_board() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)
    value = _SOLUTION[row][col]

    response = submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["accepted"] is True
    assert payload["solved"] is False
    assert payload["board"][row][col] == value


def test_submit_move_valid_move_persists_the_updated_board() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)
    value = _SOLUTION[row][col]

    submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    stored = repo.games[("game-1", "player-abc")]
    assert stored.board[row][col] == value
    assert repo.save_calls == 1


def test_submit_move_valid_move_bumps_updated_at() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(
        tracer=tracer,
        repository=repo,
        solver=Solver(tracer),
        now="2024-06-01T12:00:00+00:00",
    )
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    row, col = _empty_cell_of(_PUZZLE)
    value = _SOLUTION[row][col]

    submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    stored = repo.games[("game-1", "player-abc")]
    assert stored.updated_at == "2024-06-01T12:00:00+00:00"
    assert stored.created_at == "2024-01-01T00:00:00+00:00"


def test_submit_move_opens_the_validate_subsegment() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)
    value = _SOLUTION[row][col]

    submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    assert "validate" in tracer.entered


def test_submit_move_success_carries_cors_headers() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)
    value = _SOLUTION[row][col]

    response = submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    assert "Access-Control-Allow-Origin" in response["headers"]


def test_submit_move_response_does_not_leak_the_solution() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)
    value = _SOLUTION[row][col]

    response = submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    assert "solution" not in response["body"]
    assert "solution" not in json.loads(response["body"])


# --- solving move: completes the board ---------------------------------------


def test_submit_move_completing_move_marks_game_solved() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    game, row, col, value = _one_from_complete_game()
    _seed(repo, game)
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["accepted"] is True
    assert payload["solved"] is True
    assert payload["status"] == "solved"


def test_submit_move_completing_move_persists_solved_status() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    game, row, col, value = _one_from_complete_game()
    _seed(repo, game)
    deps = _submit_move_deps(tracer, repo)

    submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    stored = repo.games[("game-1", "player-abc")]
    assert stored.status == "solved"
    assert stored.board[row][col] == value


def test_submit_move_completing_move_opens_check_solution_subsegment() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    game, row, col, value = _one_from_complete_game()
    _seed(repo, game)
    deps = _submit_move_deps(tracer, repo)

    submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": value}), deps
    )

    assert "validate" in tracer.entered
    assert "check-solution" in tracer.entered


# --- well-formed but illegal move: 200 accepted:false, board unchanged -------


def test_submit_move_conflicting_move_is_rejected_not_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    # An empty cell, but a value that already occurs in its row -> a conflict.
    row, col = _empty_cell_of(_PUZZLE)
    conflicting = next(v for v in _SOLUTION[row] if v != _SOLUTION[row][col])

    response = submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": conflicting}), deps
    )

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["accepted"] is False
    assert payload["reason"] == "invalid_move"


def test_submit_move_rejected_move_does_not_change_the_stored_board() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)
    conflicting = next(v for v in _SOLUTION[row] if v != _SOLUTION[row][col])

    submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": conflicting}), deps
    )

    stored = repo.games[("game-1", "player-abc")]
    assert stored.board == _PUZZLE
    assert repo.save_calls == 0


def test_submit_move_onto_a_filled_cell_is_rejected() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    # A cell already given (non-zero) in the puzzle.
    row, col = 0, 1  # _PUZZLE[0][1] is a given (2)

    response = submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": 5}), deps
    )

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["accepted"] is False
    assert repo.save_calls == 0


def test_submit_move_rejected_move_opens_the_validate_subsegment() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)
    conflicting = next(v for v in _SOLUTION[row] if v != _SOLUTION[row][col])

    submit_move.handle(
        _submit_move_event(move={"row": row, "col": col, "value": conflicting}), deps
    )

    assert "validate" in tracer.entered


# --- malformed move: 400 naming the field, never 500, never writes -----------


def test_submit_move_missing_move_is_400_not_500() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(_submit_move_event(move=None), deps)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "move"
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.save_calls == 0


def test_submit_move_non_int_row_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(move={"row": "x", "col": 0, "value": 1}), deps
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "move.row"
    assert repo.save_calls == 0


def test_submit_move_row_out_of_range_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(move={"row": 9, "col": 0, "value": 1}), deps
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "move.row"
    assert repo.save_calls == 0


def test_submit_move_value_out_of_range_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(move={"row": 0, "col": 0, "value": 0}), deps
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "move.value"
    assert repo.save_calls == 0


def test_submit_move_value_bool_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(move={"row": 0, "col": 0, "value": True}), deps
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "move.value"
    assert repo.save_calls == 0


def test_submit_move_body_not_json_is_400_not_500() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(_submit_move_event(raw="{not json"), deps)

    assert response["statusCode"] == 400
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.save_calls == 0


def test_submit_move_missing_body_is_400_not_500() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle({"pathParameters": {"gameId": "game-1"}}, deps)

    assert response["statusCode"] == 400
    assert repo.save_calls == 0


# --- key validation and isolation --------------------------------------------


def test_submit_move_missing_game_id_is_400_naming_game_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(game_id=None, move={"row": 0, "col": 0, "value": 1}),
        deps,
    )

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "gameId"
    assert repo.load_calls == 0
    assert repo.save_calls == 0


def test_submit_move_missing_player_id_is_400_naming_player_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(player_id=None, move={"row": 0, "col": 0, "value": 1}),
        deps,
    )

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.save_calls == 0


def test_submit_move_unknown_item_is_404() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()  # empty
    deps = _submit_move_deps(tracer, repo)

    response = submit_move.handle(
        _submit_move_event(
            game_id="nope", player_id="player-abc", move={"row": 0, "col": 0, "value": 1}
        ),
        deps,
    )

    assert response["statusCode"] == 404
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.save_calls == 0


def test_submit_move_enforces_player_isolation_at_the_handler() -> None:
    """A game stored for playerA is 404 when a move is submitted with playerB."""
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-A"))
    deps = _submit_move_deps(tracer, repo)
    row, col = _empty_cell_of(_PUZZLE)

    response = submit_move.handle(
        _submit_move_event(
            game_id="game-1",
            player_id="player-B",
            move={"row": row, "col": col, "value": _SOLUTION[row][col]},
        ),
        deps,
    )

    assert response["statusCode"] == 404
    assert repo.save_calls == 0


# --- solve (task 8.5) --------------------------------------------------------
#
# Body shape (documented): the request body is a JSON object carrying the
# playerId, e.g. {"playerId": "player-abc"}. gameId rides the path parameters.
# This mirrors the POST /games/{gameId}/moves route, which also carries the
# playerId in the body.
#
# Behavior decision (documented): solve is READ-ONLY. It loads the game, runs
# solver.solve(board), and returns the completed board without persisting it.
# This is the one endpoint that deliberately returns a full solved board to the
# player — that is its purpose (Requirement 5.3). A board with no solution is a
# valid outcome, not an error: it maps to the no-solution response (a 200 with
# {"solved": false, "reason": "no_solution"}), per Requirement 5.4. Because
# solve never writes, the stored game is left unchanged in every case.


def _contradictory_game(
    *, game_id: str = "game-1", player_id: str = "player-abc"
) -> Game:
    """A game whose current board carries a direct row contradiction.

    The board is the givens plus a second cell in row 0 holding the same value
    as an existing given (two ``1``s in row 0), so the board admits no valid
    completion and ``solver.solve`` returns ``None``. It is otherwise a
    well-formed stored game.
    """
    board = [row[:] for row in _PUZZLE]
    # _PUZZLE[0] begins [0, 2, 3, 4, 5, 6, 7, 8, 9]; the first cell is blank.
    # Duplicate the 2 from column 1 into the blank column 0 -> two 2s in row 0.
    board[0][0] = board[0][1]
    return Game(
        game_id=game_id,
        player_id=player_id,
        board=board,
        puzzle=[row[:] for row in _PUZZLE],
        solution=[row[:] for row in _SOLUTION],
        status="in_progress",
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )


def _solve_event(
    *,
    game_id: str | None = "game-1",
    player_id: str | None = "player-abc",
    raw: str | None = None,
) -> dict[str, Any]:
    """Build a POST /games/{gameId}/solve proxy event.

    ``gameId`` rides the path parameters; ``playerId`` rides the JSON body.
    Passing ``None`` for ``game_id`` omits the path parameter; ``raw`` overrides
    the JSON body string wholesale (to exercise malformed-body paths).
    """
    path_params: dict[str, str] = {}
    if game_id is not None:
        path_params["gameId"] = game_id

    if raw is not None:
        body = raw
    else:
        payload: dict[str, Any] = {}
        if player_id is not None:
            payload["playerId"] = player_id
        body = json.dumps(payload)

    return {"pathParameters": path_params or None, "body": body}


def _solve_deps(tracer: FakeTracer, repo: InMemoryGames) -> Dependencies:
    """solve deps: a real tracer-backed Solver (fast on a near-full board)."""
    return _deps(tracer=tracer, repository=repo, solver=Solver(tracer))


# --- happy path: a solvable board returns the completed solution -------------


def test_solve_returns_200_with_a_complete_valid_solved_board() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(), deps)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["solved"] is True
    board = payload["board"]
    assert len(board) == SIZE and all(len(row) == SIZE for row in board)
    assert is_complete_and_valid(board)
    # The completed board must agree with every given of the puzzle.
    for r in range(SIZE):
        for c in range(SIZE):
            if _PUZZLE[r][c] != 0:
                assert board[r][c] == _PUZZLE[r][c]


def test_solve_completed_board_agrees_with_the_known_solution() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(), deps)

    # The puzzle has a unique solution, so the solved board is exactly _SOLUTION.
    assert json.loads(response["body"])["board"] == _SOLUTION


def test_solve_opens_the_solve_backtracking_subsegment() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    solve.handle(_solve_event(), deps)

    assert "solve-backtracking" in tracer.entered


def test_solve_success_carries_cors_headers() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(), deps)

    assert "Access-Control-Allow-Origin" in response["headers"]


def test_solve_is_read_only_and_does_not_write_the_store() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    solve.handle(_solve_event(), deps)

    # solve loads once and never persists: the stored game is untouched.
    assert repo.load_calls == 1
    assert repo.save_calls == 0
    stored = repo.games[("game-1", "player-abc")]
    assert stored.board == _PUZZLE
    assert stored.status == "in_progress"


# --- no solution: a contradictory board -> no-solution response (200) --------


def test_solve_unsolvable_board_returns_no_solution_response() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _contradictory_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(), deps)

    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["solved"] is False
    assert payload["reason"] == "no_solution"
    assert "board" not in payload
    assert "Access-Control-Allow-Origin" in response["headers"]


def test_solve_unsolvable_board_still_opens_solve_backtracking() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _contradictory_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    solve.handle(_solve_event(), deps)

    assert "solve-backtracking" in tracer.entered


def test_solve_unsolvable_board_does_not_write_the_store() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _contradictory_game(game_id="game-1", player_id="player-abc"))
    deps = _solve_deps(tracer, repo)

    solve.handle(_solve_event(), deps)

    assert repo.save_calls == 0


# --- 404, key validation, and isolation --------------------------------------


def test_solve_unknown_item_is_404() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()  # empty
    deps = _solve_deps(tracer, repo)

    response = solve.handle(
        _solve_event(game_id="nope", player_id="player-abc"), deps
    )

    assert response["statusCode"] == 404
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.save_calls == 0


def test_solve_missing_game_id_is_400_naming_game_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(game_id=None), deps)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "gameId"
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.load_calls == 0
    assert repo.save_calls == 0


def test_solve_missing_player_id_is_400_naming_player_id() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(player_id=None), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.load_calls == 0
    assert repo.save_calls == 0


def test_solve_empty_player_id_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(player_id="   "), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.load_calls == 0


def test_solve_body_not_json_is_400_not_500() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(raw="{not json"), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.load_calls == 0


def test_solve_missing_body_is_400_not_500() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _solve_deps(tracer, repo)

    response = solve.handle({"pathParameters": {"gameId": "game-1"}}, deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.load_calls == 0


def test_solve_body_not_an_object_is_400() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _solve_deps(tracer, repo)

    response = solve.handle(_solve_event(raw='"just a string"'), deps)

    assert response["statusCode"] == 400
    assert json.loads(response["body"])["field"] == "playerId"
    assert repo.load_calls == 0


def test_solve_enforces_player_isolation_at_the_handler() -> None:
    """A game stored for playerA is 404 when solve is called with playerB."""
    tracer = FakeTracer()
    repo = InMemoryGames()
    _seed(repo, _stored_game(game_id="game-1", player_id="player-A"))
    deps = _solve_deps(tracer, repo)

    response = solve.handle(
        _solve_event(game_id="game-1", player_id="player-B"), deps
    )

    assert response["statusCode"] == 404
    assert repo.save_calls == 0


# --- task 8.7: consolidated error-mapping and subsegment guarantees ----------
#
# The tests above cover, per handler, the 404/400/no-solution mappings and each
# handler's own subsegment. Task 8.7 asks for a handful of guarantees stated
# *in one place*, across the handlers, so they can't silently regress in a
# single handler without a dedicated failing test:
#
#   1. All four exact subsegment names open across a full run of the handlers.
#   2. Bad input never yields a 500 and never leaks a raw stack trace — the
#      body is always clean JSON naming the offending field.
#   3. Every error response (400 and 404) carries the Amplify-origin CORS header.
#
# These reuse the InMemoryGames / _deps / CannedSolver / _stored_game helpers
# defined above; nothing new about the wiring, only consolidated assertions.


def test_all_four_solver_subsegment_names_open_across_the_handlers() -> None:
    """Requirements 6.1–6.4: a full pass over the handlers opens, byte-for-byte,
    ``generate`` (new_game), ``validate`` (submit_move), ``check-solution``
    (a completing move), and ``solve-backtracking`` (solve).

    One shared FakeTracer records every subsegment opened across all four
    requests, so the four exact names are asserted together in a single place.
    """
    tracer = FakeTracer()
    repo = InMemoryGames()

    # new_game -> "generate" (canned solver still opens the real subsegment).
    new_game.handle(
        _post_games({"playerId": "player-abc"}),
        _deps(tracer=tracer, repository=repo, solver=CannedSolver(tracer)),
    )

    # A game one move from complete: submitting the last cell opens both
    # "validate" (checking the placement) and "check-solution" (the completed
    # board). Uses the real Solver so those subsegments are opened for real.
    completing, row, col, value = _one_from_complete_game(
        game_id="game-solve", player_id="player-abc"
    )
    repo.save(completing)
    submit_move.handle(
        _submit_move_event(
            game_id="game-solve",
            player_id="player-abc",
            move={"row": row, "col": col, "value": value},
        ),
        _deps(tracer=tracer, repository=repo, solver=Solver(tracer)),
    )

    # solve -> "solve-backtracking" on a fresh solvable game.
    repo.save(_stored_game(game_id="game-2", player_id="player-abc"))
    solve.handle(
        _solve_event(game_id="game-2", player_id="player-abc"),
        _deps(tracer=tracer, repository=repo, solver=Solver(tracer)),
    )

    assert set(tracer.entered) >= {
        "generate",
        "validate",
        "solve-backtracking",
        "check-solution",
    }
    # Every opened subsegment is also closed (the tracer records both orders).
    assert sorted(tracer.entered) == sorted(tracer.exited)


# Malformed inputs, one per handler, that must each map to 400 (never 500) with
# a clean JSON body. Each entry is (label, callable(deps) -> response, repo-seed
# needed?) — the callable builds and dispatches the event so the parametrization
# stays readable.


def _dispatch_new_game_no_body(deps: Dependencies) -> dict[str, Any]:
    return new_game.handle({}, deps)


def _dispatch_new_game_bad_json(deps: Dependencies) -> dict[str, Any]:
    return new_game.handle(_post_games(None, raw="{not json"), deps)


def _dispatch_get_game_missing_player(deps: Dependencies) -> dict[str, Any]:
    return get_game.handle(_get_game_event(player_id=None), deps)


def _dispatch_submit_move_missing_move(deps: Dependencies) -> dict[str, Any]:
    return submit_move.handle(_submit_move_event(move=None), deps)


def _dispatch_submit_move_bad_json(deps: Dependencies) -> dict[str, Any]:
    return submit_move.handle(_submit_move_event(raw="{not json"), deps)


def _dispatch_submit_move_malformed_move(deps: Dependencies) -> dict[str, Any]:
    return submit_move.handle(
        _submit_move_event(move={"row": "x", "col": 0, "value": 1}), deps
    )


def _dispatch_solve_bad_json(deps: Dependencies) -> dict[str, Any]:
    return solve.handle(_solve_event(raw="{not json"), deps)


def _dispatch_solve_missing_player(deps: Dependencies) -> dict[str, Any]:
    return solve.handle(_solve_event(player_id=None), deps)


_MALFORMED_INPUT_CASES = [
    ("new_game_missing_body", _dispatch_new_game_no_body),
    ("new_game_non_json_body", _dispatch_new_game_bad_json),
    ("get_game_missing_player_id", _dispatch_get_game_missing_player),
    ("submit_move_missing_move", _dispatch_submit_move_missing_move),
    ("submit_move_non_json_body", _dispatch_submit_move_bad_json),
    ("submit_move_malformed_move", _dispatch_submit_move_malformed_move),
    ("solve_non_json_body", _dispatch_solve_bad_json),
    ("solve_missing_player_id", _dispatch_solve_missing_player),
]


@pytest.mark.parametrize(
    "label,dispatch",
    _MALFORMED_INPUT_CASES,
    ids=[label for label, _ in _MALFORMED_INPUT_CASES],
)
def test_bad_input_is_400_never_500_and_never_leaks_a_stack_trace(
    label: str, dispatch: Any
) -> None:
    """Bad input maps to 400, never 500, and the body is a clean JSON error.

    Consolidates the "never 500, never a raw stack trace" guarantee across the
    handlers: each malformed request returns 400 (the documented client-error
    code), the body parses as JSON naming the offending field, and no response
    body carries traceback text.
    """
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=Solver(tracer))

    response = dispatch(deps)

    assert response["statusCode"] == 400
    assert response["statusCode"] != 500
    body = response["body"]
    # No raw stack trace leaked to the client.
    assert "Traceback" not in body
    assert "File \"" not in body
    # The body is clean JSON that names the offending field.
    payload = json.loads(body)
    assert isinstance(payload, dict)
    assert "field" in payload
    # Nothing was persisted on a rejected request.
    assert repo.save_calls == 0


# Error responses, one 404 and one 400 per handler that produces them, that must
# each carry the CORS header. Each callable builds and dispatches its event.


def _dispatch_get_game_404(deps: Dependencies) -> dict[str, Any]:
    return get_game.handle(
        _get_game_event(game_id="nope", player_id="player-abc"), deps
    )


def _dispatch_submit_move_404(deps: Dependencies) -> dict[str, Any]:
    return submit_move.handle(
        _submit_move_event(
            game_id="nope", player_id="player-abc", move={"row": 0, "col": 0, "value": 1}
        ),
        deps,
    )


def _dispatch_solve_404(deps: Dependencies) -> dict[str, Any]:
    return solve.handle(_solve_event(game_id="nope", player_id="player-abc"), deps)


_ERROR_RESPONSE_CASES = [
    ("new_game_400", _dispatch_new_game_no_body),
    ("get_game_400", _dispatch_get_game_missing_player),
    ("get_game_404", _dispatch_get_game_404),
    ("submit_move_400", _dispatch_submit_move_missing_move),
    ("submit_move_404", _dispatch_submit_move_404),
    ("solve_400", _dispatch_solve_missing_player),
    ("solve_404", _dispatch_solve_404),
]


@pytest.mark.parametrize(
    "label,dispatch",
    _ERROR_RESPONSE_CASES,
    ids=[label for label, _ in _ERROR_RESPONSE_CASES],
)
def test_error_responses_carry_cors_headers(label: str, dispatch: Any) -> None:
    """Requirement 8.2: every 400 and 404 response authorizes the Amplify origin.

    Consolidates the CORS-on-error guarantee: a 400 or 404 without the
    ``Access-Control-Allow-Origin`` header is unreadable to the browser, so this
    asserts the header is present on the error responses each handler produces.
    """
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=Solver(tracer))

    response = dispatch(deps)

    assert response["statusCode"] in (400, 404)
    assert "Access-Control-Allow-Origin" in response["headers"]
