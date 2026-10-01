"""Property-based tests for difficulty (feature: difficulty-levels).

Each property runs a minimum of 100 iterations and is tagged with the design's
property number. Properties over generation are parameterized by **both** a
hypothesis-driven seed **and** each registered difficulty level, so they hold
across the whole registry. Everything runs fully offline: no ``boto3``, no AWS,
no network beyond localhost.
"""

from __future__ import annotations

import json
import random
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from backend.domain.board import SIZE, Board, is_complete_and_valid, is_valid_placement
from backend.domain.difficulty import DIFFICULTY_REGISTRY, VALID_DIFFICULTY_LEVELS
from backend.domain.solver import Solver
from backend.domain.tracing import FakeTracer
from backend.handlers import new_game
from backend.handlers.bootstrap import Dependencies

_LEVELS = list(VALID_DIFFICULTY_LEVELS)


def _new_solver() -> Solver:
    return Solver(FakeTracer())


def _removal_count(level: str) -> int:
    return DIFFICULTY_REGISTRY[level].removal_count


def _blanks(board: Board) -> int:
    return sum(1 for row in board for cell in row if cell == 0)


def _count_completions(board: Board, limit: int) -> int:
    """Count completions of ``board`` independently of the Solver, capped at ``limit``."""
    working = [row[:] for row in board]
    return _count(working, limit)


def _count(board: Board, limit: int) -> int:
    cell = _first_empty_cell(board)
    if cell is None:
        return 1
    row, col = cell
    total = 0
    for value in range(1, SIZE + 1):
        if is_valid_placement(board, row, col, value):
            board[row][col] = value
            total += _count(board, limit - total)
            board[row][col] = 0
            if total >= limit:
                return total
    return total


def _first_empty_cell(board: Board) -> tuple[int, int] | None:
    for row in range(SIZE):
        for col in range(SIZE):
            if board[row][col] == 0:
                return row, col
    return None


# Feature: difficulty-levels, Property 1: Per-level removal count.
# Validates: Requirements 2.1, 3.1, 3.2
@given(seed=st.integers(), level=st.sampled_from(_LEVELS))
@settings(max_examples=100, deadline=None)
def test_property_generation_blanks_exactly_the_levels_removal_count(
    seed: int, level: str
) -> None:
    generated = _new_solver().generate(random.Random(seed), _removal_count(level))
    assert _blanks(generated.puzzle) == _removal_count(level)


# Feature: difficulty-levels, Property 3: Generated puzzle has a unique solution
# per level.
# Validates: Requirements 2.4, 2.5
@given(seed=st.integers(), level=st.sampled_from(_LEVELS))
@settings(max_examples=100, deadline=None)
def test_property_generated_puzzle_has_a_unique_solution_per_level(
    seed: int, level: str
) -> None:
    solver = _new_solver()
    generated = solver.generate(random.Random(seed), _removal_count(level))

    # Exactly one completion, counted independently of the generator's counter...
    assert _count_completions(generated.puzzle, limit=2) == 1
    # ...and it equals the solution the generator reported.
    assert solver.solve(generated.puzzle) == generated.solution


# Feature: difficulty-levels, Property 4: Every level yields a standard 9x9 board.
# Validates: Requirements 4.4
@given(seed=st.integers(), level=st.sampled_from(_LEVELS))
@settings(max_examples=100, deadline=None)
def test_property_every_level_yields_a_standard_9x9_board(
    seed: int, level: str
) -> None:
    generated = _new_solver().generate(random.Random(seed), _removal_count(level))

    for board in (generated.puzzle, generated.solution):
        assert len(board) == SIZE
        assert all(len(row) == SIZE for row in board)
    assert is_complete_and_valid(generated.solution)


# Feature: difficulty-levels, Property 7: Same seed and difficulty reproduce the
# same puzzle.
# Validates: Requirements 5.1, 5.2, 5.3, 5.4
@given(seed=st.integers(), level=st.sampled_from(_LEVELS))
@settings(max_examples=100, deadline=None)
def test_property_same_seed_and_difficulty_reproduce_the_same_puzzle(
    seed: int, level: str
) -> None:
    count = _removal_count(level)
    first = _new_solver().generate(random.Random(seed), count)
    second = _new_solver().generate(random.Random(seed), count)

    assert first.puzzle == second.puzzle
    assert first.solution == second.solution


# Feature: difficulty-levels, Property 11: The generate subsegment is opened once
# per generation for any level, by the Tracer, not the Solver's computation.
# Validates: Requirements 8.1, 8.2, 8.3
@given(seed=st.integers(), level=st.sampled_from(_LEVELS))
@settings(max_examples=100, deadline=None)
def test_property_generate_subsegment_opened_once_per_level(
    seed: int, level: str
) -> None:
    tracer = FakeTracer()
    Solver(tracer).generate(random.Random(seed), _removal_count(level))

    # Exactly one 'generate' subsegment regardless of the removal count, opened
    # and closed by the tracer (the Solver's computation opens none itself).
    assert tracer.entered == ["generate"]
    assert tracer.exited == ["generate"]


# Feature: difficulty-levels, Property 8: Invalid difficulty is rejected at the
# boundary.
# Validates: Requirements 6.1, 6.2, 6.3, 6.4


class _InMemoryGames:
    def __init__(self) -> None:
        self.games: dict[tuple[str, str], Any] = {}
        self.save_calls = 0

    def save(self, game: Any) -> None:
        self.save_calls += 1
        self.games[(game.game_id, game.player_id)] = game

    def load(self, game_id: str, player_id: str) -> Any | None:
        return self.games.get((game_id, player_id))


class _NeverCalledSolver:
    """A solver that fails the test if generation is ever reached for bad input."""

    def generate(self, rng: Any, removal_count: int) -> Any:  # pragma: no cover
        raise AssertionError("generate must not run for an invalid difficulty")


def _deps(repo: _InMemoryGames, solver: Any) -> Dependencies:
    return Dependencies(
        tracer=FakeTracer(),  # type: ignore[arg-type]
        solver=solver,
        repository=repo,  # type: ignore[arg-type]
        clock=lambda: "2024-01-01T00:00:00+00:00",
        id_source=lambda: "game-1",
        new_rng=lambda *, seed=0: random.Random(seed),
    )


# Invalid difficulty values: strings outside the valid set, and non-strings.
_invalid_strings = st.text().filter(lambda s: s not in VALID_DIFFICULTY_LEVELS)
_non_strings = st.one_of(
    st.integers(),
    st.booleans(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.none(),
    st.lists(st.sampled_from(_LEVELS)),
    st.dictionaries(st.text(), st.text()),
)
_invalid_difficulty = st.one_of(_invalid_strings, _non_strings)


@given(bad=_invalid_difficulty)
@settings(max_examples=100, deadline=None)
def test_property_invalid_difficulty_is_rejected_with_400(bad: Any) -> None:
    repo = _InMemoryGames()
    deps = _deps(repo, _NeverCalledSolver())

    event = {"body": json.dumps({"playerId": "p", "difficulty": bad})}
    response = new_game.handle(event, deps)

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "difficulty"
    assert payload["validValues"] == list(VALID_DIFFICULTY_LEVELS)
    # Never a 500, never a stack trace; CORS present; nothing stored.
    assert response["statusCode"] != 500
    assert "Traceback" not in response["body"]
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.save_calls == 0
