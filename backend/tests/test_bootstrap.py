"""Tests for the shared Lambda handler bootstrap / dependency wiring (task 8.1).

These run fully offline: importing the bootstrap module must not require AWS
credentials or network, ``patch_all`` only patches libraries (it makes no AWS
call), and the boto3-backed repository is built lazily inside the runtime
factory rather than at import, so a unit test never constructs a real resource.

The tests exercise each injectable seam on its own (clock, id source, rng, trace
id) plus the wiring factory with ``games_table_from_env`` monkeypatched to a
fake, so no test touches AWS.
"""

from __future__ import annotations

import importlib
import random
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta

import pytest

from backend.data.games_repository import GamesRepository
from backend.domain.solver import Solver
from backend.domain.tracing import Tracer, XRayTracer


def test_importing_bootstrap_module_succeeds_offline() -> None:
    """The module imports (and patches boto3) with no AWS creds or network."""
    module = importlib.import_module("backend.handlers.bootstrap")

    # A fresh import must expose the wiring surface without having touched AWS.
    assert hasattr(module, "build_runtime_dependencies")
    assert hasattr(module, "patch_boto3_for_xray")


def test_patch_boto3_for_xray_calls_patch_all_exactly_once(monkeypatch) -> None:
    """The patch wrapper delegates to aws_xray_sdk's patch_all, once per call."""
    from backend.handlers import bootstrap

    calls: list[int] = []
    monkeypatch.setattr(bootstrap, "patch_all", lambda: calls.append(1))

    bootstrap.patch_boto3_for_xray()

    assert calls == [1]


def test_system_clock_returns_iso_8601_string() -> None:
    """The real clock returns a parseable ISO-8601 UTC timestamp string."""
    from backend.handlers.bootstrap import system_clock

    now = system_clock()

    assert isinstance(now, str)
    # Round-trips through fromisoformat and carries a UTC (zero) offset.
    parsed = datetime.fromisoformat(now)
    assert parsed.tzinfo is not None
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset == timedelta(0)


def test_a_fake_clock_is_injectable() -> None:
    """A test can substitute a clock that returns a pinned timestamp."""
    from backend.handlers.bootstrap import Clock

    pinned = "2024-01-02T03:04:05+00:00"

    def fake_clock() -> str:
        return pinned

    clock: Clock = fake_clock
    assert clock() == pinned


def test_new_game_id_returns_unique_strings() -> None:
    """The real id source yields distinct, non-empty string ids."""
    from backend.handlers.bootstrap import new_game_id

    ids = {new_game_id() for _ in range(100)}

    assert len(ids) == 100
    assert all(isinstance(game_id, str) and game_id for game_id in ids)


def test_a_fake_id_source_returns_the_pinned_value() -> None:
    """A test can substitute an id source returning a fixed gameId."""
    from backend.handlers.bootstrap import IdSource

    def fake_id_source() -> str:
        return "pinned-game-id"

    id_source: IdSource = fake_id_source
    assert id_source() == "pinned-game-id"


def test_new_rng_returns_a_random_Random() -> None:
    """The rng factory returns a random.Random instance."""
    from backend.handlers.bootstrap import new_rng

    rng = new_rng()

    assert isinstance(rng, random.Random)


def test_new_rng_seeding_is_controllable_for_deterministic_tests() -> None:
    """Two equally seeded Randoms produce the same sequence (reproducibility)."""
    from backend.handlers.bootstrap import new_rng

    first = new_rng(seed=1234)
    second = new_rng(seed=1234)

    assert isinstance(first, random.Random)
    assert isinstance(second, random.Random)
    assert [first.random() for _ in range(5)] == [second.random() for _ in range(5)]


def test_current_trace_id_returns_none_without_an_active_segment() -> None:
    """Offline, with no active X-Ray segment, the helper returns None cleanly."""
    from backend.handlers.bootstrap import current_trace_id

    # No segment is open in the test process, so this must not raise.
    assert current_trace_id() is None


def test_current_trace_id_reads_the_active_segment_trace_id(monkeypatch) -> None:
    """When a segment is active, the helper returns its trace id."""
    from backend.handlers import bootstrap

    class _Segment:
        trace_id = "1-abcdef01-0123456789abcdef01234567"

    class _Recorder:
        def current_segment(self) -> _Segment:
            return _Segment()

    monkeypatch.setattr(bootstrap, "xray_recorder", _Recorder())

    assert bootstrap.current_trace_id() == "1-abcdef01-0123456789abcdef01234567"


def test_current_trace_id_swallows_recorder_errors(monkeypatch) -> None:
    """A recorder that raises when queried yields None, never an exception."""
    from backend.handlers import bootstrap

    class _Recorder:
        def current_segment(self) -> object:
            raise RuntimeError("no segment open")

    monkeypatch.setattr(bootstrap, "xray_recorder", _Recorder())

    assert bootstrap.current_trace_id() is None


def test_build_runtime_dependencies_wires_the_expected_types(monkeypatch) -> None:
    """The factory assembles real deps without touching AWS (table faked)."""
    from backend.handlers import bootstrap

    # Avoid boto3 / AWS: the factory's only AWS touchpoint is this call.
    monkeypatch.setattr(
        bootstrap, "games_table_from_env", _null_table, raising=True
    )

    deps = bootstrap.build_runtime_dependencies()

    assert isinstance(deps.tracer, XRayTracer)
    assert isinstance(deps.tracer, Tracer)
    assert isinstance(deps.solver, Solver)
    assert isinstance(deps.repository, GamesRepository)
    # Callable seams are wired to the real implementations.
    assert deps.clock is bootstrap.system_clock
    assert deps.id_source is bootstrap.new_game_id
    assert deps.new_rng is bootstrap.new_rng
    # The solver shares the wired tracer (one tracer per request bootstrap).
    assert deps.solver._tracer is deps.tracer


def test_dependencies_is_immutable() -> None:
    """The Dependencies bundle is a frozen dataclass (wiring, not mutable state)."""
    from backend.handlers.bootstrap import Dependencies

    deps = Dependencies(
        tracer=XRayTracer(),
        solver=Solver(XRayTracer()),
        repository=GamesRepository(_null_table()),
        clock=_stub_clock,
        id_source=_stub_id_source,
        new_rng=new_rng_stub,
    )

    with pytest.raises(FrozenInstanceError):
        deps.tracer = XRayTracer()  # type: ignore[misc]


def _stub_clock() -> str:
    return "t"


def _stub_id_source() -> str:
    return "id"


def new_rng_stub(*, seed: int | None = None) -> random.Random:
    return random.Random(seed)


class _NullTable:
    """A two-method table stand-in satisfying the repository's TableLike seam."""

    def get_item(self, **kwargs: object) -> dict[str, object]:
        return {}

    def put_item(self, **kwargs: object) -> dict[str, object]:
        return {}


def _null_table() -> _NullTable:
    return _NullTable()
