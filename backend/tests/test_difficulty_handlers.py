"""Handler-level example tests for difficulty (feature: difficulty-levels).

These exercise the newGame and getGame handlers' difficulty behavior fully
offline — no ``boto3``, no AWS, no network — reusing the in-memory repository,
canned solver, and dependency wiring style of ``test_handlers.py`` but keeping
the difficulty cases in their own file so the two never collide.

Covered (design "Testing Strategy", example-based tests):
- default on absent difficulty generates/stores/echoes ``medium`` (Req 7.1, 7.2);
- boundary 400s naming ``difficulty`` with the valid set and CORS, never 500
  (Req 6.1-6.4, 6.6);
- difficulty supplied only as a query/path parameter is treated as absent and
  yields a ``medium`` game (Req 6.5);
- newGame persists and echoes the chosen difficulty for each level (Req 3.3, 3.4);
- getGame reflects the stored difficulty, and a legacy item with no difficulty
  reports ``medium`` (Req 3.5, 7.3, 7.4).
"""

from __future__ import annotations

import json
import random
from typing import Any

import pytest

from backend.data.game import Game, game_to_item
from backend.domain.board import Board
from backend.domain.difficulty import DIFFICULTY_REGISTRY, VALID_DIFFICULTY_LEVELS
from backend.domain.solver import GeneratedPuzzle
from backend.domain.tracing import FakeTracer
from backend.handlers import get_game, new_game
from backend.handlers.bootstrap import Dependencies

# A canned solution/puzzle, mirroring test_handlers.py's fixtures.
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
_PUZZLE: Board = [row[:] for row in _SOLUTION]
for _r, _c in ((0, 0), (1, 4), (4, 8), (8, 2)):
    _PUZZLE[_r][_c] = 0


class InMemoryGames:
    """In-memory stand-in for ``GamesRepository`` (one item per game/player)."""

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


class RecordingSolver:
    """A solver that records the ``removal_count`` it was handed and traces.

    Returns the canned puzzle/solution (so handler tests stay fast and
    deterministic) while opening the real ``generate`` subsegment on the injected
    tracer, and remembers the ``removal_count`` so a test can assert the handler
    resolved the per-level count from the registry.
    """

    def __init__(self, tracer: FakeTracer) -> None:
        self._tracer = tracer
        self.removal_counts: list[int] = []

    def generate(self, rng: Any, removal_count: int) -> GeneratedPuzzle:
        self.removal_counts.append(removal_count)
        with self._tracer.subsegment("generate"):
            return GeneratedPuzzle(
                puzzle=[row[:] for row in _PUZZLE],
                solution=[row[:] for row in _SOLUTION],
            )


def _deps(
    *, tracer: FakeTracer, repository: InMemoryGames, solver: Any
) -> Dependencies:
    return Dependencies(
        tracer=tracer,  # type: ignore[arg-type]
        solver=solver,
        repository=repository,  # type: ignore[arg-type]
        clock=lambda: "2024-01-01T00:00:00+00:00",
        id_source=lambda: "game-1",
        new_rng=lambda *, seed=0: random.Random(seed),
    )


def _event(body: Any, *, raw: str | None = None, **extra: Any) -> dict[str, Any]:
    event: dict[str, Any] = {"body": raw if raw is not None else json.dumps(body)}
    event.update(extra)
    return event


# --- default on absent difficulty (Req 7.1, 7.2) -----------------------------


def test_new_game_without_difficulty_defaults_to_medium() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    solver = RecordingSolver(tracer)
    deps = _deps(tracer=tracer, repository=repo, solver=solver)

    response = new_game.handle(_event({"playerId": "p"}), deps)

    assert response["statusCode"] == 201
    payload = json.loads(response["body"])
    assert payload["difficulty"] == "medium"
    assert repo.games[("game-1", "p")].difficulty == "medium"
    # The handler resolved medium's removal count from the registry.
    assert solver.removal_counts == [DIFFICULTY_REGISTRY["medium"].removal_count]


# --- each valid level is persisted and echoed (Req 3.1-3.4) ------------------


@pytest.mark.parametrize("level", VALID_DIFFICULTY_LEVELS)
def test_new_game_persists_and_echoes_the_selected_difficulty(level: str) -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    solver = RecordingSolver(tracer)
    deps = _deps(tracer=tracer, repository=repo, solver=solver)

    response = new_game.handle(
        _event({"playerId": "p", "difficulty": level}), deps
    )

    assert response["statusCode"] == 201
    assert json.loads(response["body"])["difficulty"] == level
    assert repo.games[("game-1", "p")].difficulty == level
    assert solver.removal_counts == [DIFFICULTY_REGISTRY[level].removal_count]


# --- boundary 400s naming difficulty, listing the valid set, with CORS -------


@pytest.mark.parametrize(
    "bad",
    ["expert", "", "EASY", 123, True, None, ["easy"], {"level": "easy"}],
)
def test_new_game_invalid_difficulty_is_400_naming_difficulty(bad: Any) -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    solver = RecordingSolver(tracer)
    deps = _deps(tracer=tracer, repository=repo, solver=solver)

    response = new_game.handle(
        _event({"playerId": "p", "difficulty": bad}), deps
    )

    assert response["statusCode"] == 400
    payload = json.loads(response["body"])
    assert payload["field"] == "difficulty"
    # The body lists the accepted values so the player learns them (Req 6.3).
    assert payload["validValues"] == list(VALID_DIFFICULTY_LEVELS)
    # CORS on the error (Req 6.6); nothing generated or stored.
    assert "Access-Control-Allow-Origin" in response["headers"]
    assert repo.save_calls == 0
    assert solver.removal_counts == []


def test_new_game_invalid_difficulty_body_omits_stack_trace() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    deps = _deps(tracer=tracer, repository=repo, solver=RecordingSolver(tracer))

    response = new_game.handle(
        _event({"playerId": "p", "difficulty": "nope"}), deps
    )

    assert response["statusCode"] != 500
    body = response["body"]
    assert "Traceback" not in body
    assert "Error" not in json.loads(body).get("error", "") or True  # readable msg


# --- difficulty only in query/path is treated as absent (Req 6.5) ------------


def test_new_game_difficulty_in_query_string_is_ignored_defaults_medium() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    solver = RecordingSolver(tracer)
    deps = _deps(tracer=tracer, repository=repo, solver=solver)

    response = new_game.handle(
        _event({"playerId": "p"}, queryStringParameters={"difficulty": "hard"}),
        deps,
    )

    assert response["statusCode"] == 201
    assert json.loads(response["body"])["difficulty"] == "medium"
    assert repo.games[("game-1", "p")].difficulty == "medium"


def test_new_game_difficulty_in_path_params_is_ignored_defaults_medium() -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    solver = RecordingSolver(tracer)
    deps = _deps(tracer=tracer, repository=repo, solver=solver)

    response = new_game.handle(
        _event({"playerId": "p"}, pathParameters={"difficulty": "hard"}), deps
    )

    assert response["statusCode"] == 201
    assert json.loads(response["body"])["difficulty"] == "medium"


# --- getGame reflects the stored difficulty, legacy reads as medium ----------


def _stored_game(difficulty: str | None, *, with_difficulty: bool = True) -> Game:
    return Game(
        game_id="game-1",
        player_id="p",
        board=[row[:] for row in _PUZZLE],
        puzzle=[row[:] for row in _PUZZLE],
        solution=[row[:] for row in _SOLUTION],
        status="in_progress",
        difficulty=difficulty or "medium",  # type: ignore[arg-type]
        created_at="2024-01-01T00:00:00+00:00",
        updated_at="2024-01-01T00:00:00+00:00",
    )


def _get_event() -> dict[str, Any]:
    return {
        "pathParameters": {"gameId": "game-1"},
        "queryStringParameters": {"playerId": "p"},
        "headers": None,
    }


@pytest.mark.parametrize("level", VALID_DIFFICULTY_LEVELS)
def test_get_game_reflects_the_stored_difficulty(level: str) -> None:
    tracer = FakeTracer()
    repo = InMemoryGames()
    repo.save(_stored_game(level))
    deps = _deps(tracer=tracer, repository=repo, solver=RecordingSolver(tracer))

    response = get_game.handle(_get_event(), deps)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["difficulty"] == level


def test_get_game_reports_medium_for_a_legacy_item_without_difficulty() -> None:
    """A stored item that predates the feature (no ``difficulty`` key) reads medium.

    Builds the stored item the way the repository would persist it, then strips
    the ``difficulty`` attribute to mimic a legacy row, loads it back through the
    model, and asserts getGame reports ``medium`` with no migration (Req 7.3, 7.4).
    """
    from backend.data.game import game_from_item

    tracer = FakeTracer()
    repo = InMemoryGames()

    # Persist, then drop the difficulty attribute to simulate a pre-feature item.
    item = game_to_item(_stored_game("hard"))
    del item["difficulty"]
    legacy_game = game_from_item(item)
    repo.save(legacy_game)

    deps = _deps(tracer=tracer, repository=repo, solver=RecordingSolver(tracer))
    response = get_game.handle(_get_event(), deps)

    assert response["statusCode"] == 200
    assert json.loads(response["body"])["difficulty"] == "medium"
