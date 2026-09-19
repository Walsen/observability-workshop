"""Property-based tests for the Lambda handlers (task 8.6).

This file implements design.md Property 8 with hypothesis. It is kept separate
from the example-based ``test_handlers.py`` (tasks 8.2-8.5, 8.7) so the two
never collide -- this file holds only property tests.

Every test runs fully offline: no ``boto3``, no AWS credentials, no network. The
``submit_move`` handler's testable core, :func:`backend.handlers.submit_move.handle`,
is driven from a :class:`~backend.handlers.bootstrap.Dependencies` bundle wired
from test doubles -- a :class:`~backend.domain.tracing.FakeTracer`-backed
:class:`~backend.domain.solver.Solver`, an in-memory repository, and a pinned
clock and id source -- so the real domain wiring is exercised with no cloud.

Feature: xray-sudoku-demo, Property 8
"""

from __future__ import annotations

import json
import random
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.data.game import Game
from backend.domain.board import SIZE, Board
from backend.domain.solver import Solver
from backend.domain.tracing import FakeTracer
from backend.handlers import submit_move
from backend.handlers.bootstrap import Dependencies

_GAME_ID = "game-1"
_PLAYER_ID = "player-abc"
_NOW = "2024-01-01T00:00:00+00:00"


class InMemoryGames:
    """In-memory stand-in for ``GamesRepository``, one item per (game, player).

    A drop-in for :class:`~backend.data.games_repository.GamesRepository`'s
    point-access contract: ``save`` overwrites exactly that pair's item and
    ``load`` returns it or ``None``. Defined locally rather than imported from
    ``test_handlers`` so this property file has no cross-test-module dependency.
    """

    def __init__(self) -> None:
        self.games: dict[tuple[str, str], Any] = {}

    def save(self, game: Any) -> None:
        self.games[(game.game_id, game.player_id)] = game

    def load(self, game_id: str, player_id: str) -> Any | None:
        return self.games.get((game_id, player_id))


def _deps(*, tracer: FakeTracer, repository: InMemoryGames) -> Dependencies:
    """Assemble a Dependencies bundle: real tracer-backed Solver, in-memory repo."""
    return Dependencies(
        tracer=tracer,  # type: ignore[arg-type]
        solver=Solver(tracer),
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: _NOW,
        id_source=lambda: _GAME_ID,
        new_rng=lambda *, seed=0: random.Random(seed),
    )


def _submit_move_event(*, row: int, col: int, value: int) -> dict[str, Any]:
    """Build a POST /games/{gameId}/moves proxy event for the given move.

    ``gameId`` rides the path parameters; ``playerId`` and the ``move`` ride the
    JSON body, matching the documented submit_move request shape.
    """
    return {
        "pathParameters": {"gameId": _GAME_ID},
        "body": json.dumps(
            {
                "playerId": _PLAYER_ID,
                "move": {"row": row, "col": col, "value": value},
            }
        ),
    }


def _empty_cells(puzzle: Board) -> list[tuple[int, int]]:
    """Return every empty (0) cell of ``puzzle`` in row-major order."""
    return [
        (row, col)
        for row in range(SIZE)
        for col in range(SIZE)
        if puzzle[row][col] == 0
    ]


def _stored_game(puzzle: Board, solution: Board) -> Game:
    """Build an in-progress Game whose current board is the untouched puzzle."""
    return Game(
        game_id=_GAME_ID,
        player_id=_PLAYER_ID,
        board=[row[:] for row in puzzle],
        puzzle=[row[:] for row in puzzle],
        solution=[row[:] for row in solution],
        status="in_progress",
        created_at=_NOW,
        updated_at=_NOW,
    )


# Feature: xray-sudoku-demo, Property 8
# Validates: Requirements 4.4
#
# A valid move changes only its target cell. Deterministic valid moves come from
# a generated puzzle: Solver.generate(random.Random(seed)) yields a puzzle with
# empty cells plus its unique solution, so the solution's value at any empty cell
# is guaranteed a legal placement there. Hypothesis drives the seed and an index
# selecting which empty cell to fill (taken modulo the number of empty cells, so
# every draw maps to a real empty target). After submitting that move, the loaded
# board must differ from the prior puzzle in exactly that one cell.
@given(seed=st.integers(), cell_choice=st.integers(min_value=0))
@settings(max_examples=100, deadline=None)
def test_property_valid_move_changes_only_its_target_cell(
    seed: int, cell_choice: int
) -> None:
    solver = Solver(FakeTracer())
    generated = solver.generate(random.Random(seed))
    puzzle = generated.puzzle
    solution = generated.solution

    # Pick an empty cell deterministically; generate always leaves at least one.
    empties = _empty_cells(puzzle)
    assert empties, "a generated puzzle must have at least one empty cell"
    target_row, target_col = empties[cell_choice % len(empties)]
    # The solution's value at an empty cell is a guaranteed-valid placement.
    value = solution[target_row][target_col]

    tracer = FakeTracer()
    repository = InMemoryGames()
    repository.save(_stored_game(puzzle, solution))
    deps = _deps(tracer=tracer, repository=repository)

    response = submit_move.handle(
        _submit_move_event(row=target_row, col=target_col, value=value), deps
    )

    # The move is accepted (200) and the returned board carries the new value.
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["accepted"] is True
    assert payload["board"][target_row][target_col] == value

    # The persisted board differs from the prior puzzle in exactly the moved cell.
    loaded = repository.load(_GAME_ID, _PLAYER_ID)
    assert loaded is not None
    for row in range(SIZE):
        for col in range(SIZE):
            if (row, col) == (target_row, target_col):
                assert loaded.board[row][col] == value
                assert puzzle[row][col] == 0  # was empty before the move
            else:
                assert loaded.board[row][col] == puzzle[row][col]
