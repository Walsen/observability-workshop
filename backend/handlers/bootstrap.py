"""Shared Lambda bootstrap: patch boto3 for X-Ray once, wire injected deps.

The four game handlers (``new_game``, ``get_game``, ``submit_move``, ``solve`` —
tasks 8.2–8.5) share one runtime bootstrap, and it lives here so each handler
module stays a thin translation between the API Gateway proxy event and the
domain. Two jobs, kept separate per Single Responsibility:

**1. Patch boto3 for X-Ray, once, at module load.** Importing this module calls
:func:`patch_boto3_for_xray`, which delegates to ``aws_xray_sdk``'s
``patch_all``. That patches ``boto3``/``botocore`` so every DynamoDB
``GetItem``/``PutItem`` is recorded as a subsegment of the active trace
(Requirement 1.3, design "End-to-End Trace Propagation"). ``patch_all`` only
rewrites library call paths — it makes no AWS call — so importing this module is
safe with no credentials and no network. The X-Ray SDK import is confined to
this module so the domain (solver, board) stays AWS-free.

**2. Wire the injectable dependencies a handler needs.** The determinism
steering forbids ``datetime.now()``, ``uuid.uuid4()``, and module-level
``random`` inside domain logic, so time, identity, and randomness are seams the
domain receives rather than reaches for. This module defines each seam as a
narrow callable type — :data:`Clock`, :data:`IdSource`, :data:`RngFactory` —
with a real implementation (:func:`system_clock`, :func:`new_game_id`,
:func:`new_rng`) wired here at the Lambda boundary and a fake trivially
substituted in tests. :func:`build_runtime_dependencies` assembles them into a
:class:`Dependencies` bundle together with the :class:`XRayTracer`, the
tracer-sharing :class:`Solver`, and the boto3-backed :class:`GamesRepository`.

The boto3 DynamoDB resource is built **lazily inside the factory**, never at
import: constructing it needs ``GAMES_TABLE_NAME`` and could reach AWS, so a
handler unit test imports this module and injects fakes without ever calling the
factory (or monkeypatches :func:`games_table_from_env` when it does).

This module contains no domain logic — only wiring.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from aws_xray_sdk.core import patch_all, xray_recorder

from backend.data.games_repository import GamesRepository, games_table_from_env
from backend.domain.solver import Solver
from backend.domain.tracing import XRayTracer

# --- Injectable seams: the domain receives these rather than reaching for them.
#
# Each is a plain callable type so a real implementation and a test fake are
# interchangeable with no class hierarchy — the narrowest possible seam
# (Interface Segregation, Dependency Inversion).

# Returns the current time as an ISO-8601 UTC string, for a game's timestamps.
Clock = Callable[[], str]

# Returns a fresh, unique gameId string for a new game.
IdSource = Callable[[], str]

# Builds a seedable ``random.Random`` for puzzle generation. The seed is
# keyword-only and optional: production passes nothing (entropy-seeded), a test
# passes a fixed seed for reproducibility.
RngFactory = Callable[..., random.Random]


def patch_boto3_for_xray() -> None:
    """Patch boto3/botocore so DynamoDB calls become X-Ray subsegments.

    Delegates to ``aws_xray_sdk``'s ``patch_all``, which rewrites the boto3 call
    path to open a subsegment per AWS call under the active trace (Requirement
    1.3). It patches libraries only and issues no AWS call, so it is safe to run
    at import with no credentials or network. ``patch_all`` is itself idempotent;
    this module calls it exactly once at load (below).
    """
    patch_all()


def system_clock() -> str:
    """Return the current UTC time as an ISO-8601 string (the real clock).

    The single production :data:`Clock`. Uses a timezone-aware UTC ``now`` so the
    string carries an explicit offset, and lives here — not in domain logic —
    because the steering forbids the domain calling ``datetime.now()`` directly.
    """
    return datetime.now(UTC).isoformat()


def new_game_id() -> str:
    """Return a fresh unique gameId (the real :data:`IdSource`).

    A random UUID4 rendered as a string. Kept at the boundary because the
    steering forbids ``uuid.uuid4()`` inside domain logic; a handler test injects
    a pinned id instead.
    """
    return str(uuid.uuid4())


def new_rng(*, seed: int | None = None) -> random.Random:
    """Return a ``random.Random`` for puzzle generation (the real rng factory).

    With no ``seed`` the generator is entropy-seeded, as production wants; with a
    ``seed`` it is reproducible, as the generation property tests require. The
    solver takes this ``Random`` as its only source of randomness, never
    module-level ``random`` (determinism steering).
    """
    return random.Random(seed)


def current_trace_id() -> str | None:
    """Return the active X-Ray trace id for logging context, or ``None``.

    Handlers put this in the ``traceId`` log field so a log line ties back to its
    trace (design "Logging"). It reads the id off the recorder's current segment;
    offline — in a unit test with no segment open — there is no segment, so it
    returns ``None`` rather than failing. Any recorder error is swallowed to
    ``None``: a missing trace id must never break request handling or logging.
    """
    try:
        segment = xray_recorder.current_segment()
    except Exception:  # noqa: BLE001 — logging context is best-effort, never fatal
        return None
    if segment is None:
        return None
    trace_id = getattr(segment, "trace_id", None)
    return trace_id if isinstance(trace_id, str) else None


@dataclass(frozen=True)
class Dependencies:
    """The wired runtime dependencies one request's handler needs.

    A frozen bundle passed to a handler so the handler body is a thin function of
    ``(event, deps)`` — tasks 8.2–8.5 read the event, then drive these seams. In
    production :func:`build_runtime_dependencies` fills it with the real
    implementations; a handler test constructs it directly with a
    :class:`~backend.domain.tracing.FakeTracer`, an in-memory repository, and
    pinned clock/id/rng, so the handler is exercised with no AWS calls at all.
    """

    tracer: XRayTracer
    solver: Solver
    repository: GamesRepository
    clock: Clock
    id_source: IdSource
    new_rng: RngFactory


def build_runtime_dependencies() -> Dependencies:
    """Assemble the real runtime dependencies for a handler (touches AWS).

    Builds the :class:`XRayTracer`, a :class:`Solver` sharing that tracer (so its
    four subsegments land under the active trace), and the boto3-backed
    :class:`GamesRepository` from the environment-named table, plus the real
    clock, id source, and rng factory.

    The DynamoDB table is constructed here, lazily, rather than at import:
    :func:`games_table_from_env` reads ``GAMES_TABLE_NAME`` and builds a boto3
    resource, so calling this factory is the one step that needs the environment
    and AWS. Handler *unit* tests therefore avoid calling it (they inject fakes),
    and a test that does call it monkeypatches :func:`games_table_from_env`.
    """
    tracer = XRayTracer()
    return Dependencies(
        tracer=tracer,
        solver=Solver(tracer),
        repository=GamesRepository(games_table_from_env()),
        clock=system_clock,
        id_source=new_game_id,
        new_rng=new_rng,
    )


# Patch boto3 for X-Ray exactly once, at module load. Every handler module
# imports this bootstrap, so importing it on a cold start applies the patch
# before any DynamoDB call is made. Safe offline: it patches libraries only.
patch_boto3_for_xray()
