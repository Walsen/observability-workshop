"""The difficulty registry: a name-keyed, Open/Closed strategy table.

"Difficulty" in this game means *how many cells generation blanks* out of the 81
— a harder level removes more cells (fewer givens), an easier level removes
fewer (more givens). This module is the single source of truth for that mapping:
each registered level name maps to a :class:`DifficultyStrategy`, whose only
generation-relevant field is a ``removal_count``.

Keeping the levels in a name-keyed registry rather than an ``if/elif`` chain is
the Open/Closed principle in practice (design "Levels are selected by name from
an Open/Closed registry"): adding ``expert`` later is a single new
:data:`DIFFICULTY_REGISTRY` entry, and because :data:`VALID_DIFFICULTY_LEVELS`
is derived from the registry keys, no other backend code changes.

The module is pure: no ``boto3``, no ``aws_cdk``, no I/O. The Solver never
imports it — the handler resolves a strategy here at the request boundary and
passes only the resolved ``removal_count`` (an ``int``) into the Solver, keeping
the generation seam as narrow as it is today (Interface Segregation).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Literal

# The difficulty names this game accepts, as a type. Defined here, in the
# registry module, so the registry is the one place that knows the level names;
# ``backend.data.game`` imports this literal for the ``Game.difficulty`` field.
Difficulty = Literal["easy", "medium", "hard"]

# The level used wherever a difficulty is absent — on a newGame request with no
# ``difficulty`` field, and on reading a legacy item that predates this feature
# (design "Backward compatibility and defaults"). ``medium``'s removal count
# equals the pre-feature fixed value, so defaulting to it is behavior-preserving.
DEFAULT_DIFFICULTY: Difficulty = "medium"


@dataclass(frozen=True)
class DifficultyStrategy:
    """A single difficulty level's generation strategy.

    This is the Strategy object of the registry: two levels differ only in the
    ``removal_count`` they carry, never in the shape of the board they produce
    (always 9x9). ``name`` is retained for logging and for building the valid-set
    message, and is otherwise not generation-relevant.
    """

    name: Difficulty
    removal_count: int


class UnknownDifficultyError(Exception):
    """Raised by :func:`resolve` for a name outside the registry.

    Kept module-private in effect: the newGame handler catches it (or validates
    against :data:`VALID_DIFFICULTY_LEVELS` first) at the request boundary and
    maps it to a 400 naming the ``difficulty`` field — it never reaches a client
    as a 500 or a stack trace.
    """

    def __init__(self, name: object) -> None:
        super().__init__(f"Unknown difficulty: {name!r}")
        self.name = name


# The registry: an ordered, name-keyed mapping of level name -> strategy.
#
# Insertion order is easy, medium, hard — ascending removal count — so any
# iteration over the registry has a defined order (no reliance on set iteration
# or incidental ordering, per the determinism steering). The counts satisfy the
# ordering invariant easy < medium < hard, and medium == 40 equals the
# pre-feature fixed target so existing medium behavior is unchanged. Every
# accepted removal is still uniqueness-checked by the Solver, so a higher target
# never compromises the single-solution guarantee.
DIFFICULTY_REGISTRY: OrderedDict[str, DifficultyStrategy] = OrderedDict(
    (
        ("easy", DifficultyStrategy(name="easy", removal_count=30)),
        ("medium", DifficultyStrategy(name="medium", removal_count=40)),
        ("hard", DifficultyStrategy(name="hard", removal_count=50)),
    )
)

# The accepted difficulty names, in registry (ascending-count) order. Derived
# from the registry keys so adding a level stays a one-entry change: this tuple,
# the handler's validation, and the 400 body's advertised set all follow the
# registry automatically.
VALID_DIFFICULTY_LEVELS: tuple[str, ...] = tuple(DIFFICULTY_REGISTRY.keys())


def resolve(name: str) -> DifficultyStrategy:
    """Return the :class:`DifficultyStrategy` registered under ``name``.

    A pure by-name lookup — there is no ``if/elif`` chain anywhere. Raises
    :class:`UnknownDifficultyError` for a name not in :data:`DIFFICULTY_REGISTRY`,
    which the handler boundary maps to a 400.
    """
    strategy = DIFFICULTY_REGISTRY.get(name)
    if strategy is None:
        raise UnknownDifficultyError(name)
    return strategy


def is_valid_difficulty(name: object) -> bool:
    """Return whether ``name`` is a string naming a registered difficulty level.

    Used by the request boundary to validate the optional ``difficulty`` field
    before touching the domain: a non-string, or a string outside the registry,
    is rejected. Booleans are rejected even though ``bool`` subclasses ``int``
    (and are never strings here, so this is naturally handled by the str check).
    """
    return isinstance(name, str) and name in DIFFICULTY_REGISTRY
